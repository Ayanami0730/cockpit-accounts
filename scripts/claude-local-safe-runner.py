#!/usr/bin/env python3
"""Local-only controller for the unmodified official Claude CLI.

No OAuth, cookies or credential files are read here. Official CLI processes own
authentication and usage requests. Profiles are local and networking follows
the user's environment; tests inject a temporary home and synthetic CLI only.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import grp
import io
import json
import math
import os
from pathlib import Path
import pwd
import re
import selectors
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass

CURRENT_ID = "00000000-0000-4000-8000-000000000001"
MAX_JSON = 256 * 1024
MAX_WIRE = 2 * 1024 * 1024
MAX_LINE = 256 * 1024
MAX_PASSIVE_JSON = 8 * 1024 * 1024
SAFE_MESSAGES = {
    "invalid": "本地账号配置校验未通过，未执行操作。",
    "busy": "上次官方查询因账号正在使用而跳过；保留原采样时间。",
    "desktop_busy": "Claude Desktop 或 Code 正在运行，请正常退出后再切换 Desktop。",
    "login_busy": "该账号正在使用，暂不启动重新登录。",
    "network": "官方客户端检查未通过；请安装 Claude Code 后重试。",
    "signature": "Claude 客户端安装或签名未通过检查。",
    "unsupported": "此版本的官方用量协议不可用，未改用其他认证方式。",
    "timeout": "官方 Claude 查询超时，保留上次数据。",
    "signed_out": "此配置尚未登录，请通过官方 CLI 登录。",
    "auth": "无法确认官方 CLI 登录状态，未读取任何凭据文件。",
    "usage": "官方 CLI 暂未提供可用的订阅用量，保留上次数据。",
    "auth_expired": "官方客户端明确报告登录已过期；请在方便时重新登录。",
    "account_disabled": "官方客户端明确报告账号已停用或暂停；请到官方页面核对。",
    "network_failure": "官方查询遇到网络错误；这不代表账号被封禁。",
    "refreshed": "已通过官方 CLI 更新账号用量。",
    "terminal": "已打开官方 CLI 终端；请在其中完成操作。",
    "terminal_failed": "无法打开官方 CLI 终端。",
    "desktop": "已打开官方 Claude Desktop；其登录与 CLI 独立。",
    "desktop_network": "Claude Desktop 安装不可用，未启动。",
    "browser": "已打开官方用量页；浏览器当前账号可能与所选 CLI 配置不同。",
    "browser_failed": "默认浏览器未能打开官方用量页。",
    "collector_conflict": "已有自定义状态栏，未覆盖；请保留现有设置。",
    "collector": "已安装官方状态栏用量回调，等待该配置产生状态更新。",
    "collector_empty": "官方状态栏尚未提供订阅用量。",
    "inspected": "已读取当前账号的本地记录；未发起登录或用量请求。",
    "identity_unavailable": "本地账号记录无法与当前 Desktop 账号匹配，保留未知状态。",
    "identity_mismatch": "CLI 与 Desktop 本地账号记录不一致，暂不显示旧账号用量。",
    "locked": "该账号已有管理操作正在进行，请稍后再试。",
    "archived": "该配置已从账号列表移除；请先恢复到列表。",
    "archive_selected": "不能移除当前默认账号；请先明确选择另一个默认账号。",
    "archive_busy": "该配置仍有所属进程，或无法确认进程归属，暂不能移除。",
    "archive_switch_pending": "该配置仍被未完成的切换引用；请先结束该流程。",
    "failed": "本地操作未完成；账号、凭据和在用会话未被切换。",
}
ACTION_ERROR_CODES = {
    "invalid": "invalid_profile", "busy": "busy", "locked": "busy",
    "desktop_busy": "desktop_busy", "login_busy": "busy",
    "network": "unsupported_cli", "desktop_network": "network_unavailable",
    "signature": "unsupported_cli", "unsupported": "unsupported_cli",
    "signed_out": "auth_required", "auth": "auth_required",
    "collector_conflict": "collector_conflict", "timeout": "network_unavailable",
    "usage": "usage_unavailable", "browser_failed": "network_unavailable",
    "auth_expired": "auth_expired", "account_disabled": "account_disabled",
    "network_failure": "network_unavailable", "identity_mismatch": "identity_mismatch",
    "archived": "archived",
}
USAGE_ERROR_CODES = set(ACTION_ERROR_CODES.values()) | {"unknown"}


def action_error(code: str) -> dict:
    return {"ok": False, "message": SAFE_MESSAGES.get(code, SAFE_MESSAGES["failed"]),
            "code": ACTION_ERROR_CODES.get(code, "unknown")}


class SafeError(Exception):
    def __init__(self, code: str):
        self.code = code if code in SAFE_MESSAGES else "failed"
        super().__init__(SAFE_MESSAGES[self.code])


def now() -> int:
    return int(time.time())


def canonical_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise SafeError("invalid")
    try:
        parsed = str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise SafeError("invalid") from None
    if parsed != value:
        raise SafeError("invalid")
    return value


@dataclass(frozen=True)
class Policy:
    home: Path
    uid: int
    username: str
    inherited_path: str | None = None

    @classmethod
    def production(cls) -> "Policy":
        user = pwd.getpwuid(os.getuid())
        return cls(Path(user.pw_dir), os.getuid(), user.pw_name, os.environ.get("PATH", ""))

    @property
    def root(self) -> Path:
        return self.home / ".cockpit_accounts/claude_native"

    @property
    def registry(self) -> Path:
        return self.root / "registry.json"

    def executable_path(self) -> str:
        # GUI apps do not normally inherit a login shell's PATH. Include native,
        # Homebrew and common npm prefixes without sourcing shell startup files.
        paths = [str(self.home / ".local/bin"), str(self.home / ".npm-global/bin")]
        # NVM's npm prefix is normally absent from Finder's environment. Read
        # only directory names; never run a login shell or source user scripts.
        versions = self.home / ".nvm/versions/node"
        if versions.is_dir():
            def version_key(path):
                match = re.fullmatch(r"v([0-9]+)[.]([0-9]+)[.]([0-9]+)", path.name)
                return tuple(int(part) for part in match.groups()) if match else (0, 0, 0)
            installed = sorted(list(versions.iterdir())[:100], key=version_key, reverse=True)
            paths += [str(path / "bin") for path in installed if version_key(path) != (0, 0, 0)]
        if self.inherited_path is not None:
            paths += ["/opt/homebrew/bin", "/usr/local/bin"]
        paths += ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        paths += [value for value in (self.inherited_path or "").split(os.pathsep)
                  if value and Path(value).is_absolute() and ".." not in Path(value).parts]
        return os.pathsep.join(dict.fromkeys(paths))

    @property
    def cli(self) -> Path:
        for directory in self.executable_path().split(os.pathsep):
            path = Path(directory) / "claude"
            if path.is_file() and os.access(path, os.X_OK):
                return path
        # A missing path is returned for a useful, fixed preflight failure.
        return self.home / ".local/bin/claude"

    @property
    def desktop(self) -> Path:
        return next((path for path in (self.home / "Applications/Claude.app", Path("/Applications/Claude.app"))
                     if path.is_dir()), Path("/Applications/Claude.app"))

    @property
    def current_cli(self) -> Path:
        return self.home / ".claude"

    @property
    def current_desktop(self) -> Path:
        # Associating the standard CLI never imports the system Desktop account.
        return self.root / "profiles" / CURRENT_ID / "desktop"


def check_path(path: Path, policy: Policy, *, private: bool = False,
               missing: bool = False, directory: bool | None = None) -> None:
    """Reject lexical traversal, symlink ancestors and foreign-owned paths."""
    if not path.is_absolute() or ".." in path.parts or path != Path(os.path.normpath(path)):
        raise SafeError("invalid")
    try:
        relative = path.relative_to(policy.home)
    except ValueError:
        raise SafeError("invalid") from None
    cursor = policy.home
    paths = [cursor]
    for part in relative.parts:
        cursor = cursor / part
        paths.append(cursor)
    for index, current in enumerate(paths):
        try:
            info = current.lstat()
        except FileNotFoundError:
            if missing:
                return
            raise SafeError("invalid") from None
        if stat.S_ISLNK(info.st_mode) or info.st_uid != policy.uid:
            raise SafeError("invalid")
        if index < len(paths) - 1 and not stat.S_ISDIR(info.st_mode):
            raise SafeError("invalid")
        if info.st_mode & 0o022:
            raise SafeError("invalid")
        if private and (current == policy.root or policy.root in current.parents) and info.st_mode & 0o077:
            raise SafeError("invalid")
    if directory is True and not path.is_dir():
        raise SafeError("invalid")
    if directory is False and not stat.S_ISREG(path.lstat().st_mode):
        raise SafeError("invalid")


def read_json(path: Path, policy: Policy, *, private: bool = True) -> object:
    check_path(path, policy, private=private, directory=False)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != policy.uid or not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JSON:
            raise SafeError("invalid")
        data = os.read(fd, MAX_JSON + 1)
        if len(data) > MAX_JSON:
            raise SafeError("invalid")
        return json.loads(data)
    except (ValueError, UnicodeError):
        raise SafeError("invalid") from None
    finally:
        os.close(fd)


class ProjectedJSONReader:
    """Decode only a fixed metadata projection, never discarded field values.

    The tokenizer consumes unselected JSON structurally. In particular it does
    not build strings, numbers, objects or lists for token/expiry/cache fields.
    No raw source or parser diagnostics are retained or exposed.
    """
    def __init__(self, stream):
        self.stream = stream
        self.char = stream.read(1)
        self.nodes = 0

    def advance(self):
        self.char = self.stream.read(1)

    def whitespace(self):
        while self.char and self.char in " \t\r\n":
            self.advance()

    def expect(self, char):
        self.whitespace()
        if self.char != char:
            raise SafeError("identity_unavailable")
        self.advance()

    def string(self, keep):
        if self.char != '"':
            raise SafeError("identity_unavailable")
        content = ['"'] if keep else None
        self.advance()
        escaped = False
        while self.char:
            char = self.char
            if keep:
                content.append(char)
                if len(content) > 4096:
                    raise SafeError("identity_unavailable")
            self.advance()
            if not escaped and char == '"':
                return json.loads("".join(content)) if keep else None
            if ord(char) < 32:
                raise SafeError("identity_unavailable")
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
        raise SafeError("identity_unavailable")

    def value(self, schema, depth=0):
        self.whitespace()
        self.nodes += 1
        if depth > 64 or self.nodes > 200000:
            raise SafeError("identity_unavailable")
        if isinstance(schema, dict) and self.char != "{":
            raise SafeError("identity_unavailable")
        if isinstance(schema, list) and self.char != "[":
            raise SafeError("identity_unavailable")
        if schema is True and self.char in "{[":
            raise SafeError("identity_unavailable")
        if self.char == "{":
            self.advance()
            result = {} if isinstance(schema, dict) else None
            self.whitespace()
            if self.char == "}":
                self.advance()
                return result
            while True:
                self.whitespace()
                key = self.string(isinstance(schema, dict))
                self.expect(":")
                child_schema = schema.get(key) if isinstance(schema, dict) else None
                child = self.value(child_schema, depth + 1)
                if child_schema is not None:
                    if key in result:
                        raise SafeError("identity_unavailable")
                    result[key] = child
                self.whitespace()
                if self.char == "}":
                    self.advance()
                    return result
                self.expect(",")
        if self.char == "[":
            self.advance()
            result = [] if isinstance(schema, list) else None
            child_schema = schema[0] if isinstance(schema, list) else None
            self.whitespace()
            if self.char == "]":
                self.advance()
                return result
            while True:
                child = self.value(child_schema, depth + 1)
                if result is not None:
                    result.append(child)
                self.whitespace()
                if self.char == "]":
                    self.advance()
                    return result
                self.expect(",")
        if self.char == '"':
            return self.string(schema is True)
        content = [] if schema is True else None
        consumed = False
        while self.char and self.char not in ",]} \t\r\n":
            consumed = True
            if content is not None:
                content.append(self.char)
                if len(content) > 128:
                    raise SafeError("identity_unavailable")
            self.advance()
        if not consumed:
            raise SafeError("identity_unavailable")
        return json.loads("".join(content)) if content is not None else None

    def read(self, schema):
        value = self.value(schema)
        self.whitespace()
        if self.char:
            raise SafeError("identity_unavailable")
        return value


def read_projected_json(path: Path, policy: Policy, schema: dict, *, private=False) -> dict:
    check_path(path, policy, private=private, directory=False)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != policy.uid or not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PASSIVE_JSON:
            raise SafeError("identity_unavailable")
        with os.fdopen(fd, "rb", closefd=False) as binary, io.TextIOWrapper(binary, encoding="utf-8") as text:
            return ProjectedJSONReader(text).read(schema)
    except (ValueError, UnicodeError):
        raise SafeError("identity_unavailable") from None
    finally:
        os.close(fd)


def ensure_private_dir(path: Path, policy: Policy) -> None:
    if path != policy.root and policy.root not in path.parents:
        raise SafeError("invalid")
    check_path(path, policy, private=True, missing=True)
    if not path.exists():
        if path == policy.root:
            # The shared local-safe directory belongs to Rust. Bootstrap only
            # our validated child, never recursively create outside this root.
            check_path(path.parent, policy, directory=True)
        else:
            ensure_private_dir(path.parent, policy)
        path.mkdir(mode=0o700)
    check_path(path, policy, private=True, directory=True)


def atomic_write(path: Path, content: bytes, policy: Policy, *, mode: int = 0o600,
                 current_settings: bool = False) -> None:
    if current_settings:
        if path != policy.current_cli / "settings.json":
            raise SafeError("invalid")
        check_path(path.parent, policy, directory=True)
    else:
        ensure_private_dir(path.parent, policy)
    check_path(path, policy, private=not current_settings, missing=True)
    fd, temporary = tempfile.mkstemp(prefix=".claude-safe-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        check_path(path, policy, private=not current_settings, missing=True)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


class Registry:
    def __init__(self, path: str, policy: Policy):
        self.policy = policy
        if path != str(policy.registry):
            raise SafeError("invalid")
        data = read_json(policy.registry, policy)
        if (not isinstance(data, dict) or type(data.get("version")) is not int or data.get("version") != 1 or
                not isinstance(data.get("profiles"), list)):
            raise SafeError("invalid")
        if len(data["profiles"]) > 100:
            raise SafeError("invalid")
        self.profiles: dict[str, dict] = {}
        for row in data["profiles"]:
            if not isinstance(row, dict):
                raise SafeError("invalid")
            ident = canonical_uuid(row.get("id"))
            if ident in self.profiles:
                raise SafeError("invalid")
            if row.get("kind") == "current" and ident == CURRENT_ID:
                cli, desktop = policy.current_cli, policy.current_desktop
            elif row.get("kind") == "managed" and ident != CURRENT_ID:
                cli = policy.root / "profiles" / ident / "cli"
                desktop = policy.root / "profiles" / ident / "desktop"
            else:
                raise SafeError("invalid")
            if row.get("configDir") != str(cli) or row.get("desktopDir") != str(desktop):
                raise SafeError("invalid")
            row.setdefault("archived", False)
            row.setdefault("archivedAt", None)
            if not isinstance(row["archived"], bool) or (row["archivedAt"] is not None and
                    (type(row["archivedAt"]) is not int or row["archivedAt"] <= 0)):
                raise SafeError("invalid")
            for directory in (cli, desktop):
                check_path(directory, policy, private=row["kind"] == "managed", directory=True)
            self.profiles[ident] = row
        if data.get("selectedProfileId") is not None and data["selectedProfileId"] not in self.profiles:
            raise SafeError("invalid")
        if data.get("selectedProfileId") and self.profiles[data["selectedProfileId"]]["archived"]:
            raise SafeError("invalid")

    def get(self, ident: object, *, allow_archived=False) -> dict:
        ident = canonical_uuid(ident)
        if ident not in self.profiles:
            raise SafeError("invalid")
        profile = self.profiles[ident]
        if profile["archived"] and not allow_archived:
            raise SafeError("archived")
        return profile


def empty_auth() -> dict:
    return {"status": "unknown", "email": None, "plan": None, "checkedAt": None, "source": None}


def empty_health() -> dict:
    return {"status": "unknown", "reason": None, "source": "none", "observedAt": None,
            "lastSuccessfulAt": None, "lastAttemptAt": None, "lastAttemptOutcome": None}


HEALTH_STATUSES = {"unknown", "available", "auth_required", "account_disabled", "network_error", "unavailable"}
HEALTH_REASONS = {None, "supported_countries_policy", "official_notice", "auth_expired", "reauth_required", "network_failure", "query_unavailable", "officially_restored", "usage_page_available"}
HEALTH_SOURCES = {"none", "official_cli", "official_usage", "official_ui", "official_email"}


def empty_usage(error: str | None = None) -> dict:
    return {"fiveHour": None, "sevenDay": None, "extraUsage": None,
            "observedAt": None, "source": "none", "error": error, "errorCode": None}


def official_error_code(error: object) -> str:
    """Classify only explicit official error evidence; never retain raw payloads.

    These are response fields, not new endpoints. Unknown/403 responses remain
    unavailable; ordinary connectivity failures can never imply suspension.
    """
    message = error if isinstance(error, str) else error.get("message", "") if isinstance(error, dict) else ""
    code = error.get("code", error.get("type", "")) if isinstance(error, dict) else ""
    message = message.strip().lower() if isinstance(message, str) and len(message) <= 4096 else ""
    code = code.lower() if isinstance(code, str) else ""
    if re.match(r"^(?:error:\s*)?(?:your|this) (?:claude )?account (?:has been|is) (?:disabled|suspended)\b", message):
        return "account_disabled"
    if code in {"token_expired", "oauth_token_expired"} or re.match(
            r"^(?:error:\s*)?(?:your |the )?(?:oauth |access |authentication )?token (?:has |is )?expired\b", message):
        return "auth_expired"
    if code in {"authentication_error", "invalid_api_key", "invalid_token"}:
        return "auth"
    if code in {"econnreset", "econnrefused", "enotfound", "etimedout", "eai_again"} or re.match(
            r"^(?:error:\s*)?(?:connection error|network error|fetch failed|connect etimedout)\b", message):
        return "network_failure"
    return "usage"


def number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0 or value > 1e15:
        raise SafeError("unsupported")
    return value


def timestamp(value: object, *, iso: bool = False) -> int | None:
    if value is None:
        return None
    if iso:
        if not isinstance(value, str) or len(value) > 64:
            raise SafeError("unsupported")
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError()
            value = parsed.timestamp()
        except ValueError:
            raise SafeError("unsupported") from None
    value = number(value)
    if value is None or value > 32503680000:
        raise SafeError("unsupported")
    return int(value)


def usage_window(value: object, *, statusline: bool = False) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SafeError("unsupported")
    percent = number(value.get("used_percentage" if statusline else "utilization"))
    if percent is None:
        return None
    if percent > 10000:
        raise SafeError("unsupported")
    return {"usedPercent": percent, "resetsAt": timestamp(value.get("resets_at"), iso=not statusline)}


def normalize_usage(data: object, *, statusline: bool = False) -> dict:
    if not isinstance(data, dict):
        raise SafeError("unsupported")
    if not statusline and data.get("rate_limits_available") is not True:
        raise SafeError("usage")
    limits = data.get("rate_limits")
    if not isinstance(limits, dict):
        raise SafeError("collector_empty" if statusline else "usage")
    five = usage_window(limits.get("five_hour"), statusline=statusline)
    seven = usage_window(limits.get("seven_day"), statusline=statusline)
    extra = None
    if not statusline and limits.get("extra_usage") is not None:
        item = limits["extra_usage"]
        if not isinstance(item, dict) or not isinstance(item.get("is_enabled"), bool):
            raise SafeError("unsupported")
        currency = item.get("currency")
        if currency is not None and (not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency)):
            raise SafeError("unsupported")
        extra = {"enabled": item["is_enabled"], "usedMinor": number(item.get("used_credits")),
                 "limitMinor": number(item.get("monthly_limit")), "currency": currency}
    if five is None and seven is None and extra is None:
        raise SafeError("collector_empty" if statusline else "usage")
    return {"fiveHour": five, "sevenDay": seven, "extraUsage": extra, "observedAt": now(),
            "source": "official_statusline" if statusline else "official_cli", "error": None, "errorCode": None}


def normalize_auth(data: object) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("loggedIn"), bool):
        raise SafeError("auth")
    if not data["loggedIn"]:
        return {"status": "signed_out", "email": None, "plan": None, "checkedAt": now(), "source": "official_cli"}
    if data.get("apiProvider") != "firstParty" or data.get("authMethod") != "claude.ai":
        raise SafeError("auth")
    email = data.get("email")
    if email is not None and (not isinstance(email, str) or len(email) > 254 or
                              not re.fullmatch(r"[^\s@\x00-\x1f]+@[^\s@\x00-\x1f]+", email)):
        raise SafeError("auth")
    plan = data.get("subscriptionType")
    if plan not in {None, "free", "pro", "max", "team", "enterprise"}:
        plan = None
    return {"status": "signed_in", "email": email, "plan": plan, "checkedAt": now(), "source": "official_cli"}


class ProcessChannel:
    """Bounded JSONL RPC; no user frames and no raw process output escapes."""
    def __init__(self, process: subprocess.Popen, timeout: float = 35):
        self.process = process
        self.deadline = time.monotonic() + timeout
        self.selector = selectors.DefaultSelector()
        self.selector.register(process.stdout, selectors.EVENT_READ)
        self.buffer = b""
        self.total = 0

    def request(self, subtype: str, **fields: object) -> dict:
        if subtype not in {"initialize", "get_usage"}:
            raise SafeError("unsupported")
        ident = str(uuid.uuid4())
        frame = {"type": "control_request", "request_id": ident,
                 "request": {"subtype": subtype, **fields}}
        try:
            self.process.stdin.write((json.dumps(frame) + "\n").encode())
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            raise SafeError("unsupported") from None
        while True:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                if len(line) > MAX_LINE:
                    raise SafeError("unsupported")
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    raise SafeError("unsupported") from None
                if not isinstance(event, dict):
                    raise SafeError("unsupported")
                if event.get("type") == "control_request":
                    # Never grant tools, credentials or callbacks during a quota read.
                    raise SafeError("unsupported")
                if event.get("type") != "control_response":
                    continue
                response = event.get("response")
                if not isinstance(response, dict) or response.get("request_id") != ident:
                    continue
                if response.get("subtype") != "success" or not isinstance(response.get("response"), dict):
                    raise SafeError(official_error_code(response.get("error")))
                return response["response"]
            if len(self.buffer) > MAX_LINE:
                raise SafeError("unsupported")
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise SafeError("timeout")
            ready = self.selector.select(min(remaining, 1))
            if not ready:
                continue
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise SafeError("unsupported")
            self.total += len(chunk)
            if self.total > MAX_WIRE:
                raise SafeError("unsupported")
            self.buffer += chunk

    def close(self) -> None:
        self.selector.close()
        with contextlib.suppress(OSError):
            self.process.stdin.close()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            # Popen belongs to this helper; never search/kill by executable name.
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        with contextlib.suppress(OSError):
            self.process.stdout.close()


class Runner:
    def __init__(self, registry: Registry):
        self.registry = registry
        self.policy = registry.policy

    def require_active(self, profile: dict) -> dict:
        # Re-read under the caller's profile/launch lock. A queued .command or
        # helper must not use the active flag it saw before an archive commit.
        return Registry(str(self.policy.registry), self.policy).get(profile["id"])

    def base_env(self) -> dict[str, str]:
        # Do not inherit credentials, provider selection or code injection flags.
        env = {"HOME": str(self.policy.home), "USER": self.policy.username,
               "LOGNAME": self.policy.username,
               "PATH": str(self.policy.cli.parent) + os.pathsep + self.policy.executable_path(),
               "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}
        # Respect explicit user proxy settings, without choosing a proxy or region.
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                    "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
            if key in os.environ:
                env[key] = os.environ[key]
        return env

    def run_bounded(self, args: list[str], *, env: dict | None = None,
                    timeout: float = 10, input_data: bytes | None = None,
                    limit: int = MAX_JSON, cwd: Path | None = None) -> tuple[int, bytes]:
        process = subprocess.Popen(args, stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   cwd=cwd or self.policy.home, env=env or self.base_env())
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        result = bytearray()
        deadline = time.monotonic() + timeout
        try:
            if input_data is not None:
                process.stdin.write(input_data)
                process.stdin.close()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SafeError("timeout")
                if not selector.select(min(remaining, 1)):
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    return process.wait(timeout=max(0.1, remaining)), bytes(result)
                result.extend(chunk)
                if len(result) > limit:
                    raise SafeError("unsupported")
        finally:
            selector.close()
            if process.poll() is None:
                process.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=1)
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)
            process.stdout.close()
            if process.stdin is not None:
                with contextlib.suppress(OSError):
                    process.stdin.close()

    def processes(self) -> list[dict]:
        code, output = self.run_bounded(["/bin/ps", "-axo", "uid=,pid=,comm="], limit=MAX_WIRE)
        if code:
            raise SafeError("invalid")
        rows = []
        for line in output.decode("utf-8", "replace").splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) != 3 or parts[0] != str(self.policy.uid) or not parts[1].isdigit():
                continue
            executable = parts[2]
            desktop = executable.endswith("/Claude.app/Contents/MacOS/Claude")
            cli = (executable.endswith("/claude.app/Contents/MacOS/claude") or
                   executable.endswith("/resources/native-binary/claude") or
                   executable.startswith(str(self.policy.home / ".local/share/claude/versions") + "/") or
                   executable == "/opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe" or
                   Path(executable).name in {"claude", "claude.exe"})
            node = Path(executable).name in {"node", "bun"}
            if not desktop and not cli and not node:
                continue
            pid = int(parts[1])
            status, raw = self.run_bounded(["/bin/ps", "eww", "-p", str(pid), "-o", "command="], limit=MAX_WIRE)
            # Kept only in memory. Do not retain or expose command/env contents.
            text = raw.decode("utf-8", "replace")
            if node and not cli:
                if status or not raw.strip():
                    raise SafeError("invalid")
                cli = bool(re.search(r"@anthropic-ai/claude-code/(?:cli[.]js|bin/claude)|(?:^|\s)/[^\s]*[/]claude(?:\s|$)", text))
                if not cli:
                    continue
            match = re.search(r"(?:^|\s)CLAUDE_CONFIG_DIR=(.*?)(?=\s[A-Za-z_][A-Za-z0-9_]*=|$)", text)
            config = match.group(1).strip() if match else None
            rows.append({"pid": pid, "desktop": desktop, "code": cli, "executable": executable, "config": config})
        return rows

    def busy(self, profile: dict, *, desktop: bool = False) -> bool:
        processes = self.processes()
        if desktop or profile["kind"] == "current":
            return bool(processes)
        return any(row["config"] == profile["configDir"] or row["config"] is None or
                   row["executable"].startswith(profile["desktopDir"] + "/") for row in processes)

    def installation_path(self, path: Path, *, directory=False) -> Path:
        """Permit installed symlinks, but reject writable/foreign targets.

        This is separate from profile validation, which never follows symlinks.
        Native and npm installers both use symlink launchers on macOS.
        """
        try:
            resolved = path.resolve(strict=True)
            roots = [self.policy.home.resolve(), Path("/opt/homebrew"), Path("/usr/local"), Path("/Applications")]
            anchor = next(root for root in roots if resolved == root or root in resolved.parents)
            trusted_groups = {0}  # root/wheel and macOS administrators may install packages.
            with contextlib.suppress(KeyError):
                trusted_groups.add(grp.getgrnam("admin").gr_gid)
            cursor = resolved
            while True:
                info = cursor.lstat()
                if (info.st_uid not in (0, self.policy.uid) or info.st_mode & 0o002
                        or (info.st_mode & 0o020 and info.st_gid not in trusted_groups)):
                    raise SafeError("signature")
                if cursor == anchor:
                    break
                cursor = cursor.parent
            if directory:
                if not resolved.is_dir():
                    raise SafeError("signature")
            elif not resolved.is_file():
                raise SafeError("signature")
            return resolved
        except (OSError, RuntimeError, StopIteration):
            raise SafeError("signature") from None

    def verify_signature(self, path: Path, *, bundle: bool = False) -> bool:
        path = self.installation_path(path, directory=bundle)
        args = ["/usr/bin/codesign", "--verify", "--strict"]
        if bundle:
            args.append("--deep")
        code, _ = self.run_bounded([*args, str(path)], timeout=12)
        if code:
            return False
        result = subprocess.run(["/usr/bin/codesign", "-dv", str(path)], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                timeout=8, env=self.base_env())
        return result.returncode == 0 and b"TeamIdentifier=Q6L2SF6YDW" in result.stderr

    def verify_cli_installation(self, path: Path) -> str:
        resolved = self.installation_path(path)
        if not os.access(resolved, os.X_OK):
            raise SafeError("signature")
        # npm JS entrypoints are not macOS signed binaries. Check the official
        # package name and declared bin mapping; trust remains with npm install.
        if resolved.name == "cli.js" and resolved.parent.name == "claude-code" and resolved.parent.parent.name == "@anthropic-ai":
            package = self.installation_path(resolved.parent / "package.json")
            if package.stat().st_size > MAX_JSON:
                raise SafeError("signature")
            data = json.loads(package.read_text())
            if not isinstance(data, dict) or data.get("name") != "@anthropic-ai/claude-code":
                raise SafeError("signature")
            entry = data.get("bin")
            entry = entry.get("claude") if isinstance(entry, dict) else entry
            if not isinstance(entry, str) or (resolved.parent / entry).resolve() != resolved:
                raise SafeError("signature")
            return "npm"
        if not self.verify_signature(path):
            raise SafeError("signature")
        return "native"

    def preflight(self) -> dict:
        result = {"ok": False, "checkedAt": now(), "cliPath": None, "cliVersion": None, "checks": []}
        def record(label: str, good: bool, detail: str) -> None:
            result["checks"].append({"label": label, "ok": good, "detail": detail})
        try:
            cli = self.policy.cli
            installation = self.verify_cli_installation(cli)
            result["cliPath"] = str(cli)
            record("CLI 安装", True, "已核对 npm 包入口。" if installation == "npm" else "Anthropic 签名验证通过。")
            code, output = self.run_bounded([str(cli), "--version"], timeout=12, limit=512)
            match = re.fullmatch(rb"([0-9]+[.][0-9]+[.][0-9]+(?:-[A-Za-z0-9.-]+)?) \(Claude Code\)\s*", output.strip())
            if code or not match:
                raise SafeError("unsupported")
            result["cliVersion"] = match.group(1).decode("ascii")
            record("CLI 版本", True, "已读取安装版本；用量协议按响应能力检查。")
            result["ok"] = True
        except (SafeError, OSError, ValueError, subprocess.SubprocessError):
            record("客户端检查", False, "请安装官方 Claude Code，并检查本地安装权限。")
        return result

    def require_preflight(self) -> None:
        if not self.preflight()["ok"]:
            raise SafeError("signature")

    def profile_shell(self, profile: dict) -> Path:
        runtime = self.policy.root / "profiles" / profile["id"] / "runtime"
        ensure_private_dir(runtime / "zdotdir", self.policy)
        shell = runtime / "zsh"
        content = "#!/bin/zsh -f\n"
        content += "for _key in ${(k)parameters}; do\n"
        content += "  if [[ \"$_key\" == ANTHROPIC_* || \"$_key\" == HOMEBREW_* || \"$_key\" == CLAUDE_CODE_USE_* || \"$_key\" == CLAUDE_SECURESTORAGE_CONFIG_DIR || \"$_key\" == CLAUDE_CODE_OAUTH* ]]; then unset -- \"$_key\"; fi\ndone\nunset _key\n"
        content += "unset NODE_OPTIONS NODE_EXTRA_CA_CERTS NODE_TLS_REJECT_UNAUTHORIZED DYLD_INSERT_LIBRARIES SSLKEYLOGFILE CHROME_LOG_FILE CLAUDE_CONFIG_DIR\n"
        values = {"ZDOTDIR": str(runtime / "zdotdir")}
        if profile["kind"] != "current":
            values["CLAUDE_CONFIG_DIR"] = profile["configDir"]
        content += "".join(f"export {key}={shlex.quote(value)}\n" for key, value in values.items())
        content += 'exec /bin/zsh -f "$@"\n'
        atomic_write(shell, content.encode(), self.policy, mode=0o700)
        return shell

    def profile_env(self, profile: dict) -> dict[str, str]:
        env = self.base_env()
        env["SHELL"] = str(self.profile_shell(profile))
        if profile["kind"] != "current":
            env["CLAUDE_CONFIG_DIR"] = profile["configDir"]
        return env

    def snapshot_path(self, profile: dict) -> Path:
        return self.policy.root / "snapshots" / (profile["id"] + ".json")

    def health_path(self, profile: dict) -> Path:
        return self.policy.root / "health" / (profile["id"] + ".json")

    def read_health(self, profile: dict) -> dict:
        path = self.health_path(profile)
        if not path.exists():
            return empty_health()
        data = read_json(path, self.policy)
        if not isinstance(data, dict) or set(data) != set(empty_health()):
            raise SafeError("invalid")
        if data["status"] not in HEALTH_STATUSES or data["reason"] not in HEALTH_REASONS or data["source"] not in HEALTH_SOURCES:
            raise SafeError("invalid")
        if data["lastAttemptOutcome"] not in {None, "success", "busy", "auth_required", "account_disabled", "network_error", "unavailable"}:
            raise SafeError("invalid")
        for key in ("observedAt", "lastSuccessfulAt", "lastAttemptAt"):
            data[key] = timestamp(data[key])
        return data

    def save_health(self, profile: dict, health: dict) -> None:
        atomic_write(self.health_path(profile), json.dumps(health).encode(), self.policy)

    def record_health(self, profile: dict, status: str, reason: str, source: str, observed_at: int) -> dict:
        # An explicit user observation of an official notice; never email text,
        # account IDs, inferred HTTP status, or an automatic cache promotion.
        valid = ((status == "account_disabled" and reason in {"supported_countries_policy", "official_notice"}) or
                 (status == "auth_required" and reason in {"reauth_required", "auth_expired"}) or
                 (status == "available" and reason == "usage_page_available" and source == "official_ui") or
                 (status == "unknown" and reason == "officially_restored"))
        if not valid or source not in {"official_ui", "official_email"} or not isinstance(observed_at, int) or not 0 < observed_at <= now() + 60:
            raise SafeError("invalid")
        with self.lock(profile):
            self.require_active(profile)
            health = self.read_health(profile)
            if health["status"] == "account_disabled" and status != "account_disabled" and reason != "officially_restored":
                raise SafeError("account_disabled")
            if health["observedAt"] and observed_at < health["observedAt"] and (
                    status != "account_disabled" or health["reason"] == "officially_restored"):
                raise SafeError("invalid")
            health.update(status=status, reason=reason, source=source, observedAt=observed_at)
            if status == "available":
                health["lastSuccessfulAt"] = observed_at
            self.save_health(profile, health)
        return {"ok": True, "message": "已保存你核对的官方证据；没有访问账号服务。"}

    def observe_health(self, profile: dict, outcome: str, *, source="official_usage", reason=None) -> None:
        health = self.read_health(profile)
        health.update(lastAttemptAt=now(), lastAttemptOutcome=outcome)
        # Explicit suspension stays authoritative until the user records an
        # official restoration. Cached login/usage can never clear it.
        if health["status"] != "account_disabled" and outcome != "busy":
            health.update(status="available" if outcome == "success" else outcome,
                          source=source, reason=reason, observedAt=now())
        if outcome == "success":
            health["lastSuccessfulAt"] = now()
        self.save_health(profile, health)

    def read_snapshot(self, profile: dict) -> dict:
        path = self.snapshot_path(profile)
        if not path.exists():
            return {"auth": empty_auth(), "usage": empty_usage()}
        data = read_json(path, self.policy)
        if not isinstance(data, dict) or set(data) != {"auth", "usage"}:
            raise SafeError("invalid")
        auth, usage = data["auth"], data["usage"]
        if not isinstance(auth, dict) or not isinstance(usage, dict):
            raise SafeError("invalid")
        # Backward compatible with snapshots written before provenance existed.
        auth.setdefault("source", None)
        usage.setdefault("errorCode", None)
        if set(auth) != set(empty_auth()) or set(usage) != set(empty_usage()):
            raise SafeError("invalid")
        if auth["status"] not in {"unknown", "signed_in", "signed_out", "error"}:
            raise SafeError("invalid")
        if auth["email"] is not None and (not isinstance(auth["email"], str) or len(auth["email"]) > 254 or
                not re.fullmatch(r"[^\s@\x00-\x1f]+@[^\s@\x00-\x1f]+", auth["email"])):
            raise SafeError("invalid")
        if auth["plan"] not in {None, "free", "pro", "max", "team", "enterprise"}:
            raise SafeError("invalid")
        if auth["source"] not in {None, "official_cli", "cached_metadata"}:
            raise SafeError("invalid")
        auth["checkedAt"] = timestamp(auth["checkedAt"])
        if usage["source"] not in {"official_cli", "official_statusline", "desktop_cache", "none"}:
            raise SafeError("invalid")
        usage["observedAt"] = timestamp(usage["observedAt"])
        for key in ("fiveHour", "sevenDay"):
            window = usage[key]
            if window is not None:
                if not isinstance(window, dict) or set(window) != {"usedPercent", "resetsAt"}:
                    raise SafeError("invalid")
                value = number(window["usedPercent"])
                if value is None or value > 10000:
                    raise SafeError("invalid")
                usage[key] = {"usedPercent": value, "resetsAt": timestamp(window["resetsAt"])}
        extra = usage["extraUsage"]
        if extra is not None:
            if (not isinstance(extra, dict) or set(extra) != {"enabled", "usedMinor", "limitMinor", "currency"} or
                    not isinstance(extra["enabled"], bool) or
                    (extra["currency"] is not None and (not isinstance(extra["currency"], str) or
                     not re.fullmatch(r"[A-Z]{3}", extra["currency"])))):
                raise SafeError("invalid")
            usage["extraUsage"] = {"enabled": extra["enabled"], "usedMinor": number(extra["usedMinor"]),
                                   "limitMinor": number(extra["limitMinor"]), "currency": extra["currency"]}
        if usage["error"] == "该账号正在使用，已跳过刷新并保留上次数据。":
            usage.update(error=SAFE_MESSAGES["busy"], errorCode="busy")
        if usage["error"] is not None and usage["error"] not in SAFE_MESSAGES.values():
            usage["error"] = SAFE_MESSAGES["usage"]
        if usage["errorCode"] not in USAGE_ERROR_CODES | {None}:
            usage["errorCode"] = "unknown"
        if usage["error"] is None:
            usage["errorCode"] = None
        elif usage["errorCode"] is None:
            usage["errorCode"] = next((ACTION_ERROR_CODES.get(key, "unknown") for key, message in
                                       SAFE_MESSAGES.items() if message == usage["error"]), "unknown")
        return {"auth": auth, "usage": usage}

    def save_snapshot(self, profile: dict, snapshot: dict) -> None:
        atomic_write(self.snapshot_path(profile), json.dumps(snapshot, ensure_ascii=False).encode(), self.policy)

    @contextlib.contextmanager
    def lock(self, profile: dict):
        directory = self.policy.root / "locks"
        ensure_private_dir(directory, self.policy)
        path = directory / (profile["id"] + ".lock")
        check_path(path, self.policy, private=True, missing=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SafeError("locked") from None
            yield
        finally:
            os.close(fd)

    def auth_status(self, profile: dict, env: dict) -> dict:
        # Even `auth status` shares native startup, which may refresh OAuth or
        # migrate its storage. Only the explicit inactive-profile refresh path
        # calls this; passive inspect_auth never starts this subprocess.
        code, output = self.run_bounded([str(self.policy.cli), "auth", "status"], env=env, timeout=12)
        try:
            data = json.loads(output)
        except (ValueError, UnicodeError):
            raise SafeError("auth") from None
        if code not in (0, 1) or not isinstance(data, dict) or not isinstance(data.get("loggedIn"), bool):
            classified = official_error_code(data.get("error", data) if isinstance(data, dict) else data)
            raise SafeError("auth" if classified == "usage" else classified)
        result = normalize_auth(data)
        if (code == 0) != (result["status"] == "signed_in"):
            raise SafeError("auth")
        return result

    @staticmethod
    def metadata_plan(account: dict) -> str | None:
        known = {"free", "pro", "max", "team", "enterprise"}
        plan = account.get("subscriptionType")
        if isinstance(plan, str) and plan in known:
            return plan
        name = account.get("planDisplayName")
        if isinstance(name, str) and len(name) < 64:
            plain = name.strip().lower()
            if plain.startswith("claude "):
                plain = plain[7:]
            if plain in known:
                return plain
        # Payment channel, subscription creation time and token expiry never
        # imply a plan or renewal date.
        return None

    def passive_metadata(self, profile: dict) -> tuple[dict, str | None]:
        schema = {"oauthAccount": {"emailAddress": True, "accountUuid": True,
                  "organizationUuid": True, "subscriptionType": True, "planDisplayName": True}}
        identity_path = (self.policy.home / ".claude.json" if profile["kind"] == "current"
                         else Path(profile["configDir"]) / ".claude.json")
        desktop_path = Path(profile["desktopDir"]) / "config.json"
        account = read_projected_json(identity_path, self.policy, schema,
                                      private=profile["kind"] == "managed").get("oauthAccount")
        desktop = (read_projected_json(desktop_path, self.policy, {"lastKnownAccountUuid": True}, private=True)
                   if desktop_path.exists() else {})
        if not isinstance(account, dict):
            raise SafeError("identity_unavailable")
        try:
            account_id = canonical_uuid(account.get("accountUuid"))
            organization = canonical_uuid(account.get("organizationUuid"))
            desktop_id = (canonical_uuid(desktop["lastKnownAccountUuid"])
                          if desktop.get("lastKnownAccountUuid") else None)
        except SafeError:
            raise SafeError("identity_unavailable") from None
        if desktop_id is not None and account_id != desktop_id:
            raise SafeError("identity_mismatch")
        email = account.get("emailAddress")
        if (not isinstance(email, str) or len(email) > 254 or
                not re.fullmatch(r"[^\s@\x00-\x1f]+@[^\s@\x00-\x1f]+", email)):
            raise SafeError("identity_unavailable")
        return ({"status": "unknown", "email": email, "plan": self.metadata_plan(account),
                 "checkedAt": now(), "source": "cached_metadata"}, organization if desktop_id else None)

    def desktop_cached_usage(self, profile: dict, organization: str) -> dict | None:
        path = Path(profile["desktopDir"]) / "plan-usage-history.json"
        if not path.exists():
            return None
        data = read_projected_json(path, self.policy,
            {"version": True, "samples": [{"t": True, "org": True, "u": {"fh": True, "sd": True}}]},
            private=profile["kind"] == "managed")
        if data.get("version") != 2 or not isinstance(data.get("samples"), list):
            return None
        # v1 rows have no organization identity and must never be guessed.
        matches = []
        for row in data["samples"]:
            if not isinstance(row, dict) or row.get("org") != organization:
                continue
            try:
                sampled_ms = number(row.get("t"))
                if sampled_ms is None:
                    continue
                sampled_at = timestamp(sampled_ms / 1000)
                if sampled_at is None or sampled_at > now() + 300:
                    continue
                meters = row.get("u")
                if not isinstance(meters, dict):
                    continue
                five, seven = number(meters.get("fh")), number(meters.get("sd"))
                if ((five is not None and five > 10000) or (seven is not None and seven > 10000) or
                        (five is None and seven is None)):
                    continue
                matches.append((sampled_ms, sampled_at, five, seven))
            except SafeError:
                continue
        if not matches:
            return None
        _, sampled_at, five, seven = max(matches, key=lambda row: row[0])
        # Desktop persists the server's utilization percentage without scaling.
        # The history format does not retain resets or monetary extra usage.
        return {"fiveHour": {"usedPercent": five, "resetsAt": None} if five is not None else None,
                "sevenDay": {"usedPercent": seven, "resetsAt": None} if seven is not None else None,
                "extraUsage": None, "observedAt": sampled_at, "source": "desktop_cache", "error": None, "errorCode": None}

    def cached_snapshot(self, profile: dict) -> dict:
        """Pure local read for the HTTP mirror: no locks/writes/processes/network."""
        snapshot = self.read_snapshot(profile)
        try:
            auth, organization = self.passive_metadata(profile)
        except (SafeError, OSError) as error:
            # Missing metadata is not new evidence that the previous official
            # login failed. Preserve its original check time, never renew it.
            if isinstance(error, SafeError) and error.code == "identity_mismatch":
                snapshot["auth"] = empty_auth()
                snapshot["usage"] = {**empty_usage(SAFE_MESSAGES["identity_mismatch"]), "errorCode": "identity_mismatch"}
            return snapshot
        previous = snapshot["auth"]
        same_identity = (isinstance(previous.get("email"), str) and
                         previous["email"].casefold() == auth["email"].casefold())
        retained_official = previous.get("source") == "official_cli" and (
            same_identity or (previous.get("status") == "signed_out" and previous.get("email") is None))
        if not retained_official:
            snapshot["auth"] = auth
        if previous.get("email") and not same_identity:
            snapshot["usage"] = empty_usage()
        try:
            usage = self.desktop_cached_usage(profile, organization) if organization else None
        except (SafeError, OSError):
            usage = None
        if usage is not None:
            existing_time = snapshot["usage"]["observedAt"]
            # Do not replace a newer genuine statusline/CLI sample with an
            # older Desktop history row. No new matching row leaves usage intact.
            if existing_time is None or usage["observedAt"] > existing_time:
                # Keep the last query outcome separate from the new sample's
                # timestamp; a busy skip does not invalidate a passive sample.
                for key in ("error", "errorCode"):
                    usage[key] = snapshot["usage"][key]
                snapshot["usage"] = usage
        return snapshot

    def inspect_auth(self, profile: dict) -> dict:
        """Passive projection only, also safe while the existing profile is busy."""
        with self.lock(profile):
            self.require_active(profile)
            snapshot = self.cached_snapshot(profile)
            self.save_snapshot(profile, snapshot)
            if snapshot["auth"]["source"] not in {"cached_metadata", "official_cli"}:
                return action_error("identity_unavailable")
            return {"ok": True, "message": SAFE_MESSAGES["inspected"]}

    def query_usage(self, env: dict) -> dict:
        args = [str(self.policy.cli), "--print", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--no-session-persistence", "--safe-mode", "--strict-mcp-config",
                "--mcp-config", '{"mcpServers":{}}', "--tools", ""]
        process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   cwd=self.policy.home, env=env)
        channel = ProcessChannel(process)
        try:
            channel.request("initialize")
            return channel.request("get_usage", skip_behaviors=True)
        finally:
            channel.close()

    def refresh(self, profile: dict) -> dict:
        with self.lock(profile):
            self.require_active(profile)
            snapshot = self.read_snapshot(profile)
            if self.read_health(profile)["status"] == "account_disabled":
                return action_error("account_disabled")
            try:
                if self.busy(profile):
                    raise SafeError("busy")
                self.require_preflight()
                env = self.profile_env(profile)
                auth = self.auth_status(profile, env)
                previous_email = snapshot["auth"].get("email")
                if previous_email and previous_email.casefold() != (auth.get("email") or "").casefold():
                    snapshot["usage"] = empty_usage()
                snapshot["auth"] = auth
                if snapshot["auth"]["status"] != "signed_in":
                    raise SafeError("signed_out")
                snapshot["usage"] = normalize_usage(self.query_usage(env))
            except SafeError as error:
                snapshot["usage"]["error"] = SAFE_MESSAGES[error.code]
                snapshot["usage"]["errorCode"] = ACTION_ERROR_CODES.get(error.code, "unknown")
                self.save_snapshot(profile, snapshot)
                state, reason = {"busy": ("busy", None), "signed_out": ("auth_required", "reauth_required"),
                    "auth": ("auth_required", "reauth_required"), "auth_expired": ("auth_required", "auth_expired"),
                    "account_disabled": ("account_disabled", "official_notice"),
                    "network_failure": ("network_error", "network_failure"), "network": ("network_error", "network_failure"),
                    "timeout": ("network_error", "network_failure")}.get(error.code, ("unavailable", "query_unavailable"))
                self.observe_health(profile, state, reason=reason)
                return action_error(error.code)
            self.save_snapshot(profile, snapshot)
            self.observe_health(profile, "success")
            return {"ok": True, "message": SAFE_MESSAGES["refreshed"]}

    def collector_command(self, profile: dict) -> str:
        return shlex.join([sys.executable, str(Path(__file__).resolve()), "collect", "--registry",
                           str(self.policy.registry), "--profile-id", profile["id"]])

    def install_collector(self, profile: dict) -> dict:
        with self.lock(profile):
            self.require_active(profile)
            path = Path(profile["configDir"]) / "settings.json"
            data = read_json(path, self.policy, private=profile["kind"] == "managed") if path.exists() else {}
            if not isinstance(data, dict):
                raise SafeError("invalid")
            entry = {"type": "command", "command": self.collector_command(profile)}
            if "statusLine" in data and data["statusLine"] != entry:
                raise SafeError("collector_conflict")
            data["statusLine"] = entry
            atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2).encode(), self.policy,
                         current_settings=profile["kind"] == "current")
            return {"ok": True, "message": SAFE_MESSAGES["collector"]}

    def collect(self, profile: dict, raw: bytes) -> str:
        if len(raw) > MAX_JSON:
            raise SafeError("unsupported")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise SafeError("unsupported") from None
        with self.lock(profile):
            self.require_active(profile)
            snapshot = self.read_snapshot(profile)
            usage = normalize_usage(data, statusline=True)
            if snapshot["usage"]["errorCode"] in {"account_disabled", "auth_expired", "auth_required", "identity_mismatch"}:
                for key in ("error", "errorCode"):
                    usage[key] = snapshot["usage"][key]
            snapshot["usage"] = usage
            self.save_snapshot(profile, snapshot)
        five, seven = snapshot["usage"]["fiveHour"], snapshot["usage"]["sevenDay"]
        return "Claude 5h " + (f"{five['usedPercent']:g}%" if five else "?") + " · 7d " + (f"{seven['usedPercent']:g}%" if seven else "?")

    def terminal(self, profile: dict, action: str) -> dict:
        with self.lock(profile):
            self.require_active(profile)
            launcher = self._prepare_terminal(profile, action)
        # The Terminal child takes this same lock and re-reads the registry at
        # exec. Do not hold its lock while asking Terminal to start the child.
        code, _ = self.run_bounded(["/usr/bin/open", "-a", "Terminal", str(launcher)], timeout=8)
        if code:
            raise SafeError("terminal_failed")
        return {"ok": True, "message": SAFE_MESSAGES["terminal"]}

    def _prepare_terminal(self, profile: dict, action: str) -> Path:
        if self.read_health(profile)["status"] == "account_disabled":
            raise SafeError("account_disabled")
        if action == "login_cli" and self.busy(profile):
            raise SafeError("login_busy")
        self.require_preflight()
        self.profile_shell(profile)
        runtime = self.policy.root / "launchers"
        ensure_private_dir(runtime, self.policy)
        launcher = runtime / (profile["id"] + "-" + action + ".command")
        command = shlex.join([sys.executable, "-I", "-B", str(Path(__file__).resolve()), action, "--registry",
                              str(self.policy.registry), "--profile-id", profile["id"], "--terminal-child"])
        atomic_write(launcher, ("#!/bin/sh\nexec " + command + "\n").encode(), self.policy, mode=0o700)
        return launcher

    def terminal_child(self, profile: dict, action: str) -> None:
        with self.lock(profile):
            self.require_active(profile)
            self._terminal_child(profile, action)

    def _terminal_child(self, profile: dict, action: str) -> None:
        if action not in {"login_cli", "launch_cli"}:
            raise SafeError("invalid")
        if self.read_health(profile)["status"] == "account_disabled":
            raise SafeError("account_disabled")
        if action == "login_cli" and self.busy(profile):
            raise SafeError("login_busy")
        self.require_preflight()
        env = self.profile_env(profile)
        args = [str(self.policy.cli)]
        if action == "login_cli":
            args += ["auth", "login"]
        os.execve(str(self.policy.cli), args, env)

    def launch_desktop(self, profile: dict) -> dict:
        # One lock for all Desktop profiles, not one per target account.
        with self.lock({"id": CURRENT_ID}):
            return self._launch_desktop(profile)

    def _launch_desktop(self, profile: dict) -> dict:
        self.require_active(profile)
        if self.read_health(profile)["status"] == "account_disabled":
            raise SafeError("account_disabled")
        if self.busy(profile, desktop=True):
            raise SafeError("desktop_busy")
        self.require_preflight()
        if not self.verify_signature(self.policy.desktop, bundle=True):
            raise SafeError("signature")
        env = self.profile_env(profile)
        env["CLAUDE_USER_DATA_DIR"] = profile["desktopDir"]
        if self.busy(profile, desktop=True):
            raise SafeError("desktop_busy")
        subprocess.Popen([str(self.policy.desktop / "Contents/MacOS/Claude"),
            "--user-data-dir=" + profile["desktopDir"]], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=self.policy.home, env=env,
            start_new_session=True)
        return {"ok": True, "message": SAFE_MESSAGES["desktop"]}

    def open_usage(self) -> dict:
        code, _ = self.run_bounded(["/usr/bin/open", "https://claude.ai/settings/usage"], timeout=15)
        if code:
            raise SafeError("browser_failed")
        return {"ok": True, "message": SAFE_MESSAGES["browser"]}


def main(argv: list[str] | None = None) -> int:
    class SafeParser(argparse.ArgumentParser):
        def error(self, message):
            raise SafeError("invalid")
    parser = SafeParser(add_help=False)
    parser.add_argument("action", choices=["preflight", "refresh", "inspect_auth", "login_cli", "launch_cli", "launch_desktop",
                                          "open_usage", "install_collector", "collect", "record_health"])
    parser.add_argument("--registry", required=True)
    parser.add_argument("--profile-id")
    parser.add_argument("--terminal-child", action="store_true")
    parser.add_argument("--health-status")
    parser.add_argument("--health-reason")
    parser.add_argument("--health-source")
    parser.add_argument("--observed-at", type=int)
    args = None
    try:
        args = parser.parse_args(argv)
        policy = Policy.production()
        runner = Runner(Registry(args.registry, policy))
        if args.terminal_child and args.action not in {"login_cli", "launch_cli"}:
            raise SafeError("invalid")
        if args.action == "preflight":
            result = runner.preflight()
        else:
            profile = runner.registry.get(args.profile_id)
            if args.terminal_child:
                runner.terminal_child(profile, args.action)
                return 0
            if args.action == "collect":
                text = runner.collect(profile, sys.stdin.buffer.read(MAX_JSON + 1))
                print(text)
                return 0
            if args.action == "record_health":
                result = runner.record_health(profile, args.health_status, args.health_reason, args.health_source, args.observed_at)
            elif args.action == "refresh":
                result = runner.refresh(profile)
            elif args.action == "inspect_auth":
                result = runner.inspect_auth(profile)
            elif args.action in {"login_cli", "launch_cli"}:
                result = runner.terminal(profile, args.action)
            elif args.action == "launch_desktop":
                result = runner.launch_desktop(profile)
            elif args.action == "install_collector":
                result = runner.install_collector(profile)
            else:
                with runner.lock(profile):
                    runner.require_active(profile)
                    result = runner.open_usage()
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (SafeError, OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        code = error.code if isinstance(error, SafeError) else "failed"
        if args and args.action == "collect":
            print("Claude 用量暂不可用")
        elif args and args.action == "preflight":
            print(json.dumps({"ok": False, "checkedAt": now(), "cliPath": None, "cliVersion": None,
                              "checks": [{"label": "本地配置", "ok": False, "detail": SAFE_MESSAGES[code]}]}, ensure_ascii=False))
        else:
            print(json.dumps(action_error(code), ensure_ascii=False))
        return 0


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main())
