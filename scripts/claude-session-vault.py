#!/usr/bin/env python3
"""Preserve Claude session bytes without reading or moving authentication.

The importable API accepts only known current/managed profile paths. Inventory is
read-only. Snapshots and handoff receipts live exclusively in an owner-only vault
outside the checkout. Handoff never modifies Desktop ownership indexes, never
overwrites divergent content, and never starts Claude. A strictly newer complete
JSONL may replace its prefix after both profiles are protected. No hashes are used.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import stat
import struct
import subprocess
import time
import uuid
from dataclasses import dataclass

CURRENT_ID = "00000000-0000-4000-8000-000000000001"
CLI_SCOPES = ("projects", "file-history", "todos", "tasks", "plans")
DESKTOP_SCOPES = ("claude-code-sessions", "local-agent-mode-sessions")
DENIED_NAMES = {
    "auth", "oauth", "credentials", ".credentials.json", "auth.json", ".claude.json",
    "config.json", "settings.json", "claude_desktop_config.json", "cookies", "cookies-journal",
    "keychain", "keychains", "session-env", ".ssh", ".env", "local state",
}
MAX_JSON = 32 * 1024 * 1024
MAX_FILES = 200_000
MESSAGES = {
    "path": "会话路径、权限或符号链接检查未通过，未移动会话。",
    "profile": "配置不属于已知的个人或独立 Claude 目录。",
    "active": "源配置或目标配置仍在使用，或无法确认其空闲；未复制或切换会话。",
    "session": "没有找到完整可续接的所选 CLI 会话；原始记录保持不变。",
    "conflict": "目标已有同名记录或附件，未覆盖任何文件。",
    "changing": "会话文件仍在变化或未完整写入；原始字节已保留，暂不续接。",
    "backup": "会话保全未完整完成，未修改源记录。",
    "rollback": "目标文件在复制后已变化或正在使用，未执行回退。",
    "receipt": "会话保全凭据无效或不属于本工具。",
}


class VaultError(Exception):
    def __init__(self, code: str):
        self.code = code if code in MESSAGES else "backup"
        super().__init__(MESSAGES[self.code])


def now() -> int:
    return int(time.time())


def canonical_uuid(value) -> str:
    if not isinstance(value, str):
        raise VaultError("session")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise VaultError("session") from None
    if str(parsed) != value or parsed.int == 0:
        raise VaultError("session")
    return value


def safe_text(value, limit=240):
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value[:limit] if value else None


def safe_cwd(value):
    if not isinstance(value, str) or not value.startswith("/") or len(value) > 4096:
        return None
    if any(ord(c) < 32 for c in value):
        return None
    return value


def file_state(info):
    return {"device": info.st_dev, "inode": info.st_ino, "size": info.st_size,
            "mtimeNs": info.st_mtime_ns, "ctimeNs": info.st_ctime_ns}


@dataclass(frozen=True)
class VaultPolicy:
    home: Path
    uid: int

    @classmethod
    def production(cls):
        account = pwd.getpwuid(os.getuid())
        return cls(Path(account.pw_dir), os.getuid())

    @property
    def native(self):
        return self.home / ".cockpit_accounts/claude_native"

    @property
    def backups(self):
        return self.native / "session-backups"

    @property
    def current_cli(self):
        return self.home / ".claude"

    @property
    def current_desktop(self):
        return self.native / "profiles" / CURRENT_ID / "desktop"


def check_path(path: Path, policy: VaultPolicy, *, private=False, missing=True):
    if not path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise VaultError("path")
    try:
        relative = path.relative_to(policy.home)
    except ValueError:
        raise VaultError("path") from None
    current = policy.home
    for part in (None, *relative.parts):
        if part is not None:
            current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if missing:
                continue
            raise VaultError("path") from None
        if stat.S_ISLNK(info.st_mode) or info.st_uid != policy.uid or info.st_mode & 0o022:
            raise VaultError("path")
        if current != path and not stat.S_ISDIR(info.st_mode):
            raise VaultError("path")
        if private and (current == policy.backups or policy.backups in current.parents) and info.st_mode & 0o077:
            raise VaultError("path")


def private_mkdir(path: Path, policy: VaultPolicy):
    check_path(path, policy, private=True)
    current = policy.home
    created = []
    for part in path.relative_to(policy.home).parts:
        current = current / part
        if not current.exists():
            current.mkdir(mode=0o700)
            created.append(current)
        if not current.is_dir():
            raise VaultError("path")
    check_path(path, policy, private=True, missing=False)
    return created


def read_small_json(path: Path, policy: VaultPolicy):
    check_path(path, policy, missing=False)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != policy.uid or info.st_size > MAX_JSON:
            raise VaultError("path")
        try:
            return json.loads(stream.read(MAX_JSON + 1))
        except (ValueError, UnicodeError):
            raise VaultError("session") from None


def atomic_json(path: Path, value, policy: VaultPolicy):
    private_mkdir(path.parent, policy)
    check_path(path, policy, private=True)
    temp = path.parent / (".new-" + str(uuid.uuid4()))
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2).encode())
            stream.flush()
            os.fsync(stream.fileno())
        check_path(path, policy, private=True)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def denied(part: str) -> bool:
    name = part.lower()
    return name in DENIED_NAMES or name.startswith(("ant-device", "ant-did")) or name.endswith((".key", ".pem", ".p12", ".pfx"))


def stable_file(path: Path, policy: VaultPolicy, destination: Path | None = None, *, after_chunk=None):
    """Read every byte to EOF once; preserve unfinished tails verbatim in copies."""
    check_path(path, policy, missing=False)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    output = None
    result = {"bytes": 0, "stable": False, "parseStatus": "not_applicable", "validLines": 0,
              "invalidLines": 0, "incompleteTail": False, "validationSkipped": False,
              "cwd": None, "sessionId": None}
    jsonl = path.suffix.lower() == ".jsonl"
    regular_json = path.suffix.lower() == ".json"
    buffer = b""

    def parse_line(raw, tail=False):
        if not raw.strip():
            return
        try:
            row = json.loads(raw)
        except (ValueError, UnicodeError):
            if tail:
                result["incompleteTail"] = True
            else:
                result["invalidLines"] += 1
            return
        result["validLines"] += 1
        if isinstance(row, dict):
            cwd = safe_cwd(row.get("cwd"))
            if cwd:
                result["cwd"] = cwd
            session = row.get("sessionId")
            if isinstance(session, str):
                try:
                    result["sessionId"] = canonical_uuid(session)
                except VaultError:
                    pass

    try:
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_uid != policy.uid:
                raise VaultError("path")
            result["sourceBefore"] = file_state(before)
            if destination is not None:
                private_mkdir(destination.parent, policy)
                check_path(destination, policy, private=True)
                output = os.fdopen(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb")
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                result["bytes"] += len(chunk)
                if output is not None:
                    output.write(chunk)
                if after_chunk:
                    after_chunk(path, result["bytes"])
                if jsonl and not result["validationSkipped"]:
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        parse_line(line)
                    if len(buffer) > MAX_JSON:
                        result["validationSkipped"] = True
                        buffer = b""
                elif regular_json and not result["validationSkipped"]:
                    buffer += chunk
                    if len(buffer) > MAX_JSON:
                        result["validationSkipped"] = True
                        buffer = b""
            after = os.fstat(stream.fileno())
            try:
                named = path.lstat()
                result["stable"] = file_state(before) == file_state(after) == file_state(named)
            except OSError:
                result["stable"] = False
            result["sourceAfter"] = file_state(after)
            if jsonl and not result["validationSkipped"]:
                if buffer:
                    parse_line(buffer, tail=True)
                    # Even a parseable final JSON value without its delimiter can
                    # still be in the middle of an append; don't claim a sealed log.
                    result["incompleteTail"] = True
                result["parseStatus"] = "invalid" if result["invalidLines"] else "incomplete" if result["incompleteTail"] else "valid"
            elif regular_json and not result["validationSkipped"]:
                try:
                    json.loads(buffer)
                    result["parseStatus"] = "valid"
                except (ValueError, UnicodeError):
                    result["parseStatus"] = "invalid"
                    result["invalidLines"] = 1
            if result["validationSkipped"]:
                result["parseStatus"] = "unverified"
        if output is not None:
            output.flush()
            os.fsync(output.fileno())
    finally:
        if output is not None:
            output.close()
    return result


def compare_bytes(source: Path, target: Path, policy: VaultPolicy):
    """Exact bounded-memory comparison; no digest and no transcript output."""
    for path in (source, target):
        check_path(path, policy, missing=False)
    with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW), "rb") as left, \
         os.fdopen(os.open(target, os.O_RDONLY | os.O_NOFOLLOW), "rb") as right:
        before = [os.fstat(left.fileno()), os.fstat(right.fileno())]
        if any(not stat.S_ISREG(info.st_mode) or info.st_uid != policy.uid for info in before):
            raise VaultError("path")
        remaining = min(before[0].st_size, before[1].st_size)
        identical_prefix = True
        while remaining:
            count = min(1024 * 1024, remaining)
            a, b = left.read(count), right.read(count)
            if len(a) != count or len(b) != count:
                raise VaultError("changing")
            if a != b:
                identical_prefix = False
                break
            remaining -= count
        after = [os.fstat(left.fileno()), os.fstat(right.fileno())]
        named = [source.lstat(), target.lstat()]
        if any(file_state(a) != file_state(b) or file_state(a) != file_state(c) for a, b, c in zip(before, after, named)):
            raise VaultError("changing")
        if not identical_prefix:
            return "diverged"
        if before[0].st_size == before[1].st_size:
            return "identical"
        return "target_prefix" if before[0].st_size > before[1].st_size else "source_prefix"


def proc_allowlist(pid: int):
    """Never return raw argv/env: only profile paths and session UUID candidates."""
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        mib = (ctypes.c_int * 3)(1, 49, pid)
        size = ctypes.c_size_t(1024 * 1024)
        raw = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 3, raw, ctypes.byref(size), None, 0):
            return None
        data = raw.raw[:size.value]
        argc = struct.unpack_from("i", data)[0]
        pos = data.index(b"\0", 4) + 1
        while pos < len(data) and data[pos] == 0:
            pos += 1
        args = []
        for _ in range(argc):
            end = data.index(b"\0", pos)
            args.append(data[pos:end].decode("utf-8", "replace"))
            pos = end + 1
        answer = {"configDir": None, "desktopDir": None, "sessionIds": set(),
                  "claudeCli": any("@anthropic-ai/claude-code/" in arg or
                                   (arg.startswith("/") and Path(arg).name == "claude") for arg in args)}
        for entry in data[pos:].split(b"\0"):
            if entry.startswith(b"CLAUDE_CONFIG_DIR="):
                answer["configDir"] = entry.split(b"=", 1)[1].decode("utf-8", "replace")
        for index, arg in enumerate(args):
            if arg.startswith("--user-data-dir="):
                answer["desktopDir"] = arg.split("=", 1)[1]
            if arg in {"--resume", "--session-id"} and index + 1 < len(args):
                try:
                    answer["sessionIds"].add(canonical_uuid(args[index + 1]))
                except VaultError:
                    pass
        return answer
    except (OSError, ValueError, struct.error):
        return None


def inspect_activity(profile: dict, policy: VaultPolicy):
    try:
        result = subprocess.run(["/bin/ps", "-axo", "uid=,pid=,comm="], capture_output=True, timeout=5)
        if result.returncode:
            raise OSError()
        busy, known, sessions = False, True, set()
        for line in result.stdout.decode("utf-8", "replace").splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) != 3 or parts[0] != str(policy.uid):
                continue
            _, pid, executable = parts
            desktop = executable.endswith("/Claude.app/Contents/MacOS/Claude")
            code = Path(executable).name in {"claude", "claude.exe"} or "/.local/share/claude/versions/" in executable
            node = Path(executable).name in {"node", "bun"}
            if not desktop and not code and not node:
                continue
            metadata = proc_allowlist(int(pid))
            if metadata is None:
                known, busy = False, True
                continue
            if node and not code and not metadata.get("claudeCli"):
                continue
            exact = metadata["configDir"] == profile["configDir"] or metadata["desktopDir"] == profile["desktopDir"]
            if exact:
                busy = True
                sessions.update(metadata["sessionIds"])
            elif not metadata["configDir"] and not metadata["desktopDir"]:
                # An unclassified live Claude may own either profile. Fail closed.
                known, busy = False, True
        return {"known": known, "busy": busy, "sessionIds": sessions}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"known": False, "busy": True, "sessionIds": set()}


class SessionVault:
    def __init__(self, policy: VaultPolicy | None = None, activity_probe=None):
        self.policy = policy or VaultPolicy.production()
        self.activity_probe = activity_probe or inspect_activity

    def validate_profile(self, profile):
        if not isinstance(profile, dict):
            raise VaultError("profile")
        ident = canonical_uuid(profile.get("id"))
        if profile.get("kind") == "current" and ident == CURRENT_ID:
            cli, desktop = self.policy.current_cli, self.policy.current_desktop
        elif profile.get("kind") == "managed" and ident != CURRENT_ID:
            base = self.policy.native / "profiles" / ident
            cli, desktop = base / "cli", base / "desktop"
        else:
            raise VaultError("profile")
        if profile.get("configDir") != str(cli) or profile.get("desktopDir") != str(desktop):
            raise VaultError("profile")
        for path in (cli, desktop):
            check_path(path, self.policy)
        return {"id": ident, "kind": profile["kind"], "configDir": str(cli), "desktopDir": str(desktop)}

    def _files(self, profile):
        files, skipped = [], {"symlinksSkipped": 0, "excludedFiles": 0}
        for scope, root, directories in (("cli", Path(profile["configDir"]), CLI_SCOPES),
                                         ("desktop", Path(profile["desktopDir"]), DESKTOP_SCOPES)):
            for name in directories:
                base = root / name
                if not base.exists() and not base.is_symlink():
                    continue
                if base.is_symlink():
                    skipped["symlinksSkipped"] += 1
                    continue
                check_path(base, self.policy, missing=False)
                if not base.is_dir():
                    raise VaultError("path")
                stack = [base]
                while stack:
                    directory = stack.pop()
                    for path in sorted(directory.iterdir()):
                        if denied(path.name):
                            skipped["excludedFiles"] += 1
                            continue
                        info = path.lstat()
                        if stat.S_ISLNK(info.st_mode):
                            skipped["symlinksSkipped"] += 1
                            continue
                        check_path(path, self.policy, missing=False)
                        if stat.S_ISDIR(info.st_mode):
                            stack.append(path)
                        elif stat.S_ISREG(info.st_mode):
                            files.append({"scope": scope, "relativePath": str(path.relative_to(root)), "path": path})
                            if len(files) > MAX_FILES:
                                raise VaultError("backup")
        return sorted(files, key=lambda f: (f["scope"], f["relativePath"])), skipped

    @staticmethod
    def _belongs(row, project, session):
        if row["scope"] != "cli":
            return False
        parts = Path(row["relativePath"]).parts
        if parts[:2] == ("projects", project):
            return (len(parts) == 3 and parts[2] == session + ".jsonl") or (len(parts) >= 4 and parts[2] in {session, "memory"})
        if len(parts) > 1 and parts[0] in {"file-history", "tasks"}:
            return parts[1] == session
        if len(parts) > 1 and parts[0] == "todos":
            return parts[1] == session or parts[1].startswith(session + "-")
        return parts[0] == "plans"

    @staticmethod
    def _summary(rows, skipped):
        return {"files": len(rows), "bytes": sum(r["bytes"] for r in rows),
                "jsonlFiles": sum(r["relativePath"].endswith(".jsonl") for r in rows),
                "incompleteFiles": sum(r["incompleteTail"] for r in rows),
                "invalidFiles": sum(bool(r["invalidLines"]) for r in rows),
                "unstableFiles": sum(not r["stable"] for r in rows),
                "unverifiedFiles": sum(r["validationSkipped"] for r in rows), **skipped}

    def inventory(self, profile):
        profile = self.validate_profile(profile)
        files, skipped = self._files(profile)
        activity = self.activity_probe(profile, self.policy)
        rows, sessions = [], []
        desktop_titles = {}
        for item in files:
            row = {k: v for k, v in item.items() if k != "path"}
            row.update(stable_file(item["path"], self.policy))
            rows.append(row)
            if item["scope"] == "desktop" and item["path"].name.startswith("local_") and item["path"].suffix == ".json":
                try:
                    data = read_small_json(item["path"], self.policy)
                    ident = canonical_uuid(data.get("cliSessionId", ""))
                    desktop_titles[ident] = {"label": safe_text(data.get("title")), "cwd": safe_cwd(data.get("cwd"))}
                except (VaultError, AttributeError):
                    pass
        for row in rows:
            parts = Path(row["relativePath"]).parts
            if row["scope"] != "cli" or len(parts) != 3 or parts[0] != "projects" or not parts[2].endswith(".jsonl"):
                continue
            try:
                ident = canonical_uuid(parts[2][:-6])
            except VaultError:
                continue
            title = None
            custom = Path(profile["configDir"]) / "projects" / parts[1] / ident / "custom-title.json"
            if custom.exists():
                try:
                    title = safe_text(read_small_json(custom, self.policy).get("customTitle"))
                except (VaultError, AttributeError):
                    pass
            related = [r for r in rows if self._belongs(r, parts[1], ident)]
            exact_active = ident in activity["sessionIds"]
            state = "active" if exact_active else "possibly_active" if activity["busy"] else "inactive"
            cwd = row["cwd"] or desktop_titles.get(ident, {}).get("cwd")
            sessions.append({"sessionId": ident, "label": title or desktop_titles.get(ident, {}).get("label") or safe_text(Path(cwd).name if cwd else parts[1]),
                "cwd": cwd, "projectKey": parts[1],
                "updatedAt": row["sourceAfter"]["mtimeNs"] // 1_000_000_000,
                "active": state != "inactive", "activity": state, "parseStatus": row["parseStatus"],
                "stable": row["stable"], "files": len(related), "bytes": sum(r["bytes"] for r in related)})
        return {"checkedAt": now(), "profileId": profile["id"], "active": activity["busy"],
                "activityKnown": activity["known"], "sessions": sorted(sessions, key=lambda s: s["updatedAt"], reverse=True),
                "summary": self._summary(rows, skipped),
                "warning": "Desktop 索引仅保全；CLI 续接与 Desktop 官方续接须分别验证。"}

    @contextlib.contextmanager
    def _lock(self):
        private_mkdir(self.policy.backups, self.policy)
        path = self.policy.backups / ".vault.lock"
        check_path(path, self.policy, private=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            yield

    def _new_receipt_dir(self):
        ident = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + str(uuid.uuid4())
        root = self.policy.backups / ident
        private_mkdir(root, self.policy)
        return ident, root

    def _snapshot(self, profile):
        ident, root = self._new_receipt_dir()
        rows = []
        files, skipped = self._files(profile)
        for item in files:
            destination = root / "data" / item["scope"] / item["relativePath"]
            record = stable_file(item["path"], self.policy, destination)
            rows.append({"scope": item["scope"], "relativePath": item["relativePath"], **record,
                         "storedState": file_state(destination.lstat())})
        summary = self._summary(rows, skipped)
        receipt = {"version": 1, "kind": "snapshot", "receiptId": ident, "createdAt": now(),
                   "profile": profile, "summary": summary, "files": rows,
                   "warning": "活跃文件可能继续追加；保留原始字节，未截断或修复源文件。"}
        atomic_json(root / "receipt.json", receipt, self.policy)
        return {"ok": True, "backupId": ident, "backupPath": str(root), "receiptPath": str(root / "receipt.json"), **summary}

    def snapshot(self, profile):
        profile = self.validate_profile(profile)
        with self._lock():
            return self._snapshot(profile)

    def _idle(self, *profiles):
        for profile in profiles:
            activity = self.activity_probe(profile, self.policy)
            if activity["busy"] or not activity["known"]:
                raise VaultError("active")

    def safe_resume_plan(self, profile, session_id):
        """Prepare only; the caller must run its normal client/launch/busy guards."""
        profile = self.validate_profile(profile)
        ident = canonical_uuid(session_id)
        self._idle(profile)
        inventory = self.inventory(profile)
        selected = next((s for s in inventory["sessions"] if s["sessionId"] == ident), None)
        if selected is None or selected["parseStatus"] != "valid" or not selected["stable"] or not selected["cwd"]:
            raise VaultError("session")
        files, skipped = self._files(profile)
        if skipped["symlinksSkipped"]:
            raise VaultError("path")
        for item in files:
            if not self._belongs(item, selected["projectKey"], ident):
                continue
            state = stable_file(item["path"], self.policy)
            if not state["stable"] or state["invalidLines"] or state["incompleteTail"] or state["validationSkipped"]:
                raise VaultError("changing")
        self._idle(profile)
        return {"ok": True, "profileId": profile["id"], "sessionId": ident, "cwd": selected["cwd"],
                "args": ["--resume", ident], "warning": "仅准备官方 CLI 续接参数；尚未启动程序或发送模型消息。"}

    def _load_receipt(self, ident):
        if not isinstance(ident, str) or not re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f-]{36}", ident):
            raise VaultError("receipt")
        canonical_uuid(ident[17:])
        path = self.policy.backups / ident / "receipt.json"
        check_path(path, self.policy, private=True, missing=False)
        receipt = read_small_json(path, self.policy)
        if not isinstance(receipt, dict) or receipt.get("version") != 1 or receipt.get("receiptId") != ident:
            raise VaultError("receipt")
        return receipt, path

    def handoff(self, source, target, session_ids, *, receipt_callback=None, backup_callback=None):
        source, target = self.validate_profile(source), self.validate_profile(target)
        if source["id"] == target["id"] or not isinstance(session_ids, list) or not session_ids:
            raise VaultError("profile")
        wanted = list(dict.fromkeys(canonical_uuid(value) for value in session_ids))
        with self._lock():
            self._idle(source, target)
            inventory = self.inventory(source)
            selected = [s for s in inventory["sessions"] if s["sessionId"] in wanted]
            if len(selected) != len(wanted) or any(s["parseStatus"] != "valid" or not s["stable"] or not s["cwd"] for s in selected):
                raise VaultError("session")
            files, skipped = self._files(source)
            if skipped["symlinksSkipped"]:
                raise VaultError("path")
            items = [f for f in files if any(self._belongs(f, s["projectKey"], s["sessionId"]) for s in selected)]
            destinations = [Path(target["configDir"]) / f["relativePath"] for f in items]
            for path in destinations:
                check_path(path, self.policy)
            backup = self._snapshot(source)
            if backup_callback is not None:
                backup_callback(backup["backupId"], None)
            target_backup = self._snapshot(target)
            if backup_callback is not None:
                backup_callback(backup["backupId"], target_backup["backupId"])
            archived, _ = self._load_receipt(backup["backupId"])
            target_archived, _ = self._load_receipt(target_backup["backupId"])
            archived_rows = {(r["scope"], r["relativePath"]): r for r in archived["files"]}
            target_rows = {(r["scope"], r["relativePath"]): r for r in target_archived["files"]}
            plan = []
            for item in items:
                r = archived_rows[(item["scope"], item["relativePath"])]
                if not r["stable"] or r["invalidLines"] or r["incompleteTail"] or r["validationSkipped"]:
                    raise VaultError("changing")
                check_path(item["path"], self.policy, missing=False)
                if file_state(item["path"].lstat()) != r["sourceAfter"]:
                    raise VaultError("changing")
                destination = Path(target["configDir"]) / item["relativePath"]
                old = target_rows.get(("cli", item["relativePath"]))
                operation, relation = "created", "new"
                if old is not None:
                    if not old["stable"] or file_state(destination.lstat()) != old["sourceAfter"]:
                        raise VaultError("changing")
                    archived_path = Path(backup["backupPath"]) / "data/cli" / item["relativePath"]
                    old_path = Path(target_backup["backupPath"]) / "data/cli" / item["relativePath"]
                    relation = compare_bytes(archived_path, old_path, self.policy)
                    if relation == "identical":
                        operation = "reused"
                    elif Path(item["relativePath"]).suffix == ".jsonl" and relation == "target_prefix":
                        operation = "replaced"
                    elif Path(item["relativePath"]).suffix == ".jsonl" and relation == "source_prefix":
                        if old["invalidLines"] or old["incompleteTail"] or old["validationSkipped"]:
                            raise VaultError("changing")
                        operation = "reused"
                    else:
                        raise VaultError("conflict")
                elif destination.exists() or destination.is_symlink():
                    raise VaultError("conflict")
                plan.append({"relativePath": item["relativePath"], "operation": operation, "relation": relation,
                             "beforeState": old["sourceAfter"] if old else None})
            self._idle(source, target)
            ident, root = self._new_receipt_dir()
            receipt = {"version": 1, "kind": "handoff", "receiptId": ident, "createdAt": now(),
                "source": source, "target": target, "sessionIds": wanted, "sourceBackupId": backup["backupId"],
                "targetBackupId": target_backup["backupId"], "plan": plan,
                "status": "prepared", "files": [], "createdDirectories": [], "publishing": None, "unpublished": []}
            atomic_json(root / "receipt.json", receipt, self.policy)
            if receipt_callback is not None:
                receipt_callback(receipt)
            try:
                for item, destination, planned in zip(items, destinations, plan):
                    self._idle(source, target)
                    check_path(destination, self.policy)
                    if planned["beforeState"] is not None:
                        if not destination.exists() or file_state(destination.lstat()) != planned["beforeState"]:
                            raise VaultError("changing")
                    elif destination.exists() or destination.is_symlink():
                        raise VaultError("conflict")
                    if planned["operation"] == "reused":
                        receipt["files"].append({**planned, "bytes": destination.stat().st_size, "state": file_state(destination.lstat())})
                        atomic_json(root / "receipt.json", receipt, self.policy)
                        continue
                    for directory in private_mkdir(destination.parent, self.policy):
                        if Path(target["configDir"]) == directory or Path(target["configDir"]) in directory.parents:
                            receipt["createdDirectories"].append(str(directory.relative_to(Path(target["configDir"]))))
                    check_path(destination, self.policy)
                    if planned["operation"] == "created" and (destination.exists() or destination.is_symlink()):
                        raise VaultError("conflict")
                    archived_path = Path(backup["backupPath"]) / "data" / "cli" / item["relativePath"]
                    staging = destination.parent / (".session-new-" + str(uuid.uuid4()))
                    try:
                        copied = stable_file(archived_path, self.policy, staging)
                        if not copied["stable"]:
                            raise VaultError("changing")
                        self._idle(source, target)
                        receipt["publishing"] = {**planned, "expectedState": file_state(staging.lstat()), "bytes": copied["bytes"]}
                        atomic_json(root / "receipt.json", receipt, self.policy)
                        if planned["operation"] == "created":
                            # Atomic no-clobber publication for new files.
                            os.link(staging, destination, follow_symlinks=False)
                            staging.unlink()
                        else:
                            check_path(destination, self.policy, missing=False)
                            if file_state(destination.lstat()) != planned["beforeState"]:
                                raise VaultError("changing")
                            os.replace(staging, destination)
                    except FileExistsError:
                        receipt["unpublished"].append(item["relativePath"])
                        receipt["publishing"] = None
                        raise VaultError("conflict") from None
                    finally:
                        staging.unlink(missing_ok=True)
                    receipt["files"].append({**planned, "bytes": copied["bytes"], "state": file_state(destination.lstat())})
                    receipt["publishing"] = None
                    atomic_json(root / "receipt.json", receipt, self.policy)
                receipt["status"] = "copied"
                atomic_json(root / "receipt.json", receipt, self.policy)
            except Exception as error:
                receipt["status"] = "partial"
                receipt["failureCode"] = error.code if isinstance(error, VaultError) else "io_error" if isinstance(error, OSError) else "unexpected"
                receipt["failurePhase"] = "publication"
                atomic_json(root / "receipt.json", receipt, self.policy)
                # Keep every published byte and its receipt for explicit rollback;
                # never delete behind a newly active session.
                return {"ok": False, "receiptId": ident, "receiptPath": str(root / "receipt.json"),
                    "files": len(receipt["files"]), "bytes": sum(r["bytes"] for r in receipt["files"]),
                    "sessionIds": wanted, "resumeArguments": [], "failureCode": receipt["failureCode"],
                    "warning": "复制未完整完成；保留了回退凭据，未启动或切换任何会话。"}
            return {"ok": True, "receiptId": ident, "receiptPath": str(root / "receipt.json"),
                "files": len(receipt["files"]), "bytes": sum(r["bytes"] for r in receipt["files"]), "sessionIds": wanted,
                "createdFiles": sum(r["operation"] == "created" for r in receipt["files"]),
                "replacedFiles": sum(r["operation"] == "replaced" for r in receipt["files"]),
                "reusedFiles": sum(r["operation"] == "reused" for r in receipt["files"]),
                "resumeArguments": [{"sessionId": s["sessionId"], "cwd": s["cwd"], "args": ["--resume", s["sessionId"]]} for s in selected],
                "warning": "已保全双端并准备 CLI 增量接续，尚未调用模型或验证续接；Desktop 索引未改写账号归属。"}

    def rollback(self, receipt_id):
        with self._lock():
            receipt, path = self._load_receipt(receipt_id)
            if receipt.get("kind") == "handoff" and receipt.get("status") == "rolled_back":
                return {"ok": True, "receiptId": receipt_id, "removedFiles": 0, "restoredFiles": 0,
                        "message": "此回执已经回退；未重复改动任何文件。"}
            if receipt.get("kind") != "handoff" or receipt.get("status") not in {"prepared", "copied", "partial", "rollback_partial"}:
                raise VaultError("receipt")
            target = self.validate_profile(receipt.get("target"))
            self._idle(target)
            backup, _ = self._load_receipt(receipt.get("targetBackupId"))
            if backup.get("kind") != "snapshot" or backup.get("profile") != target:
                raise VaultError("receipt")
            backup_rows = {(r["scope"], r["relativePath"]): r for r in backup["files"]}
            targets = []
            restoring = receipt.get("rollingBack")
            if restoring:
                row = next((r for r in receipt["files"] if r["relativePath"] == restoring["relativePath"]), None)
                if row is None:
                    raise VaultError("receipt")
                relative = Path(row["relativePath"])
                if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] not in CLI_SCOPES or any(denied(p) for p in relative.parts):
                    raise VaultError("receipt")
                destination = Path(target["configDir"]) / relative
                check_path(destination, self.policy)
                actual = file_state(destination.lstat()) if destination.exists() else None
                expected = restoring.get("expectedState")
                completed = row["operation"] == "created" and actual is None
                if row["operation"] == "replaced" and actual and expected and all(actual[k] == expected[k] for k in ("device", "inode", "size", "mtimeNs")):
                    archived = self.policy.backups / receipt["targetBackupId"] / "data/cli" / relative
                    completed = compare_bytes(archived, destination, self.policy) == "identical"
                if completed:
                    row["rolledBack"] = True
                elif actual != row["state"]:
                    raise VaultError("rollback")
                receipt["rollingBack"] = None
                atomic_json(path, receipt, self.policy)
            pending = receipt.get("publishing")
            if pending:
                relative = Path(pending["relativePath"])
                if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] not in CLI_SCOPES or any(denied(p) for p in relative.parts):
                    raise VaultError("receipt")
                destination = Path(target["configDir"]) / relative
                check_path(destination, self.policy)
                actual = file_state(destination.lstat()) if destination.exists() else None
                expected = pending["expectedState"]
                if actual and all(actual[k] == expected[k] for k in ("device", "inode", "size", "mtimeNs")):
                    archived = self.policy.backups / receipt["sourceBackupId"] / "data/cli" / relative
                    if compare_bytes(archived, destination, self.policy) != "identical":
                        raise VaultError("rollback")
                    receipt["files"].append({k: v for k, v in {**pending, "state": actual}.items() if k != "expectedState"})
                elif (pending["operation"] == "created" and actual is None) or actual == pending["beforeState"]:
                    receipt.setdefault("unpublished", []).append(pending["relativePath"])
                else:
                    raise VaultError("rollback")
                receipt["publishing"] = None
                atomic_json(path, receipt, self.policy)
            recorded = {row["relativePath"] for row in receipt["files"]}
            # A process crash between publication and its per-file receipt leaves
            # ownership uncertain. Never pretend that rollback removed it.
            for item in receipt.get("plan", []):
                if item["relativePath"] in recorded or item["relativePath"] in receipt.get("unpublished", []):
                    continue
                relative = Path(item["relativePath"])
                if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] not in CLI_SCOPES or any(denied(p) for p in relative.parts):
                    raise VaultError("receipt")
                destination = Path(target["configDir"]) / relative
                check_path(destination, self.policy)
                if (item["operation"] == "created" and destination.exists()) or (item["operation"] == "replaced" and
                        (not destination.exists() or file_state(destination.lstat()) != item["beforeState"])):
                    raise VaultError("rollback")
            for row in receipt["files"]:
                operation = row.get("operation")
                if operation not in {"created", "replaced", "reused"}:
                    raise VaultError("receipt")
                if operation == "reused" or row.get("rolledBack"):
                    continue
                relative = Path(row["relativePath"])
                if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] not in CLI_SCOPES or any(denied(p) for p in relative.parts):
                    raise VaultError("receipt")
                destination = Path(target["configDir"]) / relative
                check_path(destination, self.policy, missing=False)
                if file_state(destination.lstat()) != row["state"]:
                    raise VaultError("rollback")
                old_path = None
                if operation == "replaced":
                    old = backup_rows.get(("cli", row["relativePath"]))
                    if old is None:
                        raise VaultError("receipt")
                    old_path = self.policy.backups / receipt["targetBackupId"] / "data/cli" / relative
                    check_path(old_path, self.policy, private=True, missing=False)
                    if file_state(old_path.lstat()) != old.get("storedState"):
                        raise VaultError("rollback")
                targets.append((destination, row, old_path))
            directories = []
            for relative in sorted(set(receipt.get("createdDirectories", [])), key=lambda p: len(Path(p).parts), reverse=True):
                parts = Path(relative)
                if parts.is_absolute() or ".." in parts.parts or (parts.parts and parts.parts[0] not in CLI_SCOPES):
                    raise VaultError("receipt")
                directory = Path(target["configDir"]) / relative
                check_path(directory, self.policy)
                directories.append(directory)
            self._idle(target)
            removed, restored = 0, 0
            try:
                for destination, row, old_path in targets:
                    self._idle(target)
                    check_path(destination, self.policy, missing=False)
                    if file_state(destination.lstat()) != row["state"]:
                        raise VaultError("rollback")
                    if row["operation"] == "created":
                        receipt["rollingBack"] = {"relativePath": row["relativePath"], "expectedState": None}
                        atomic_json(path, receipt, self.policy)
                        destination.unlink()
                        removed += 1
                    else:
                        staging = destination.parent / (".session-restore-" + str(uuid.uuid4()))
                        try:
                            restored_data = stable_file(old_path, self.policy, staging)
                            if not restored_data["stable"] or file_state(destination.lstat()) != row["state"]:
                                raise VaultError("rollback")
                            receipt["rollingBack"] = {"relativePath": row["relativePath"], "expectedState": file_state(staging.lstat())}
                            atomic_json(path, receipt, self.policy)
                            os.replace(staging, destination)
                            restored += 1
                        finally:
                            staging.unlink(missing_ok=True)
                    row["rolledBack"] = True
                    receipt["rollingBack"] = None
                    atomic_json(path, receipt, self.policy)
            except Exception:
                receipt["status"] = "rollback_partial"
                atomic_json(path, receipt, self.policy)
                raise VaultError("rollback") from None
            for directory in directories:
                try:
                    directory.rmdir()
                except OSError:
                    pass
            receipt["status"] = "rolled_back"
            receipt["rolledBackAt"] = now()
            atomic_json(path, receipt, self.policy)
            return {"ok": True, "receiptId": receipt_id, "removedFiles": removed, "restoredFiles": restored,
                    "message": "仅撤回此次新增或替换且未被再改动的目标文件；原会话和双端保护备份保留。"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inventory-current", "snapshot-current"))
    args = parser.parse_args()
    vault = SessionVault()
    profile = {"id": CURRENT_ID, "kind": "current", "configDir": str(vault.policy.current_cli), "desktopDir": str(vault.policy.current_desktop)}
    try:
        if args.action == "snapshot-current":
            answer = vault.snapshot(profile)
        else:
            result = vault.inventory(profile)
            # CLI deliberately omits session IDs, labels, cwd and the full table.
            answer = {"checkedAt": result["checkedAt"], "active": result["active"], "activityKnown": result["activityKnown"],
                      "sessionCount": len(result["sessions"]), "summary": result["summary"], "warning": result["warning"]}
        print(json.dumps(answer, ensure_ascii=False))
        return 0
    except (VaultError, OSError):
        print(json.dumps({"ok": False, "message": MESSAGES["backup"]}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
