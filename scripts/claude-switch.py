#!/usr/bin/env python3
"""User-initiated, receipted Claude Desktop switch. Never a background switcher.

All paths come from the registered profiles. Tests inject a temporary policy,
vault and lifecycle; importing this module never inspects the real account.
"""
import argparse
import contextlib
import importlib.util
import json
import errno
import os
from pathlib import Path
import pty
import selectors
import subprocess
import sys
import time
import uuid


def sibling(name, filename):
    path = Path(__file__).resolve().with_name(filename)
    info = path.lstat()
    if path.is_symlink() or not path.is_file() or info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022:
        raise RuntimeError("Unavailable local helper")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


safe = sibling("claude_switch_safe", "claude-local-safe-runner.py")
vault_api = sibling("claude_switch_vault", "claude-session-vault.py")
TERMINAL = {"complete", "rolled_back", "cancelled", "abandoned"}
STAGES = {"planned", "waiting_exit", "waiting_login", "copying", "copy_incomplete", "copied", "launching",
          "waiting_desktop", "registering", "registration_uncertain", "awaiting_verification", "confirming",
          "rollback_waiting_exit", "failed"} | TERMINAL
MESSAGES = {
    "planned": "切换计划已保存；尚未退出客户端或复制文件。",
    "waiting_exit": "仍有相关进程。请先结束任务，再明确请求正常退出；不会强制结束。",
    "waiting_login": "目标账号需要官方登录；已有历史与默认账号保持原样。",
    "copying": "正在备份两端并复制保存的历史；回执已持久保存。",
    "copy_incomplete": "文件交接未全部完成。请使用回退入口；不会重复覆盖或重新复制。",
    "copied": "历史文件交接完成，尚未验收 Desktop。",
    "launching": "已请求以目标隔离目录启动官方 Desktop。",
    "waiting_desktop": "等待目标 Desktop 启动；尚未登记会话。",
    "registering": "正在通过官方 Desktop 入口逐条登记历史，不发送模型提示。",
    "registration_uncertain": "上一条登记结果不确定。请核对界面后确认已出现，或明确重试该条。",
    "awaiting_verification": "登记已完成；请在官方界面核对账号、项目、历史及模型设置。默认账号尚未改变。",
    "confirming": "界面验收已记录，正在保存默认账号。",
    "complete": "目标身份与会话登记验收已记录，Cockpit 默认账号已更新。没有发送新模型消息。",
    "rollback_waiting_exit": "回退前须正常退出目标客户端；只撤回本次写入且之后未变化的文件。",
    "rolled_back": "本次未再变化的文件已回退，源记录与双端备份保留。",
    "cancelled": "切换已取消，没有改动默认账号。已退出的客户端不会被自动重启。",
    "abandoned": "已保留全部当前文件与备份并结束本次流程；没有执行回退。默认账号维持当前值。",
    "failed": "此阶段未完成；回执已保留。可重试阶段或在满足条件后回退。",
}


def register_with_pty(cli, session_id, cwd, env, *, timeout=45):
    """Only the official registration frontend is owned by this bounded PTY.

    Both streams must be TTYs for the CLI Desktop handoff. Output is
    discarded, never echoed or retained. Desktop/Code descendants are untouched.
    """
    safe.canonical_uuid(session_id)
    master, slave = pty.openpty()
    process = None
    selector = selectors.DefaultSelector()
    try:
        process = subprocess.Popen([str(cli), "--desktop", "--resume", session_id], stdin=slave, stdout=slave, stderr=slave,
                                   cwd=cwd, env=env, start_new_session=True)
        os.close(slave); slave = -1
        selector.register(master, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        total = 0
        while time.monotonic() < deadline:
            if selector.select(min(0.25, max(0, deadline - time.monotonic()))):
                try:
                    chunk = os.read(master, 65536)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    chunk = b""
                if not chunk:
                    return process.wait(timeout=max(0.1, deadline - time.monotonic()))
                total += len(chunk)
                if total > safe.MAX_WIRE:
                    raise safe.SafeError("unsupported")
            elif process.poll() is not None:
                return process.returncode
        raise safe.SafeError("timeout")
    finally:
        selector.close()
        if slave >= 0: os.close(slave)
        os.close(master)
        if process is not None and process.poll() is None:
            process.terminate()  # This exact Popen child only; never a process group.
            try: process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=2)


class Lifecycle:
    def __init__(self, runner):
        self.runner = runner

    def runtime(self, source, target):
        result = {"source": False, "target": False, "sourceDesktop": False, "targetDesktop": False, "other": False, "known": True}
        for row in self.runner.processes():
            meta = vault_api.proc_allowlist(row["pid"])
            if meta is None:
                result["known"] = False
                continue
            matched = False
            for key, profile in (("source", source), ("target", target)):
                if meta["configDir"] == profile["configDir"] or meta["desktopDir"] == profile["desktopDir"]:
                    result[key] = True
                    result[key + "Desktop"] |= row["desktop"]
                    matched = True
            if not matched:
                result["other"] = True
                if not meta["configDir"] and not meta["desktopDir"]:
                    result["known"] = False
        return result

    def quit(self, source, target, *, rollback=False):
        state = self.runtime(source, target)
        owned, foreign = ("target", "source") if rollback else ("source", "target")
        if not state["known"] or state["other"] or state[foreign]:
            raise safe.SafeError("busy")
        if not state[owned]:
            return
        if not state[owned + "Desktop"]:
            return  # Standalone CLI processes must be closed by their owner.
        if not self.runner.verify_signature(self.runner.policy.desktop, bundle=True):
            raise safe.SafeError("signature")
        # A standard quit Apple event, never Quit Anyway, signals or process-name
        # termination. The official application's own task warning stays visible.
        script = "tell application " + json.dumps(str(self.runner.policy.desktop)) + " to quit"
        self.runner.run_bounded(["/usr/bin/osascript", "-e", script], timeout=8)

    def launch(self, target):
        # SwitchManager already holds the global Desktop and both profile locks.
        return self.runner._launch_desktop(target)

    def register(self, target, session):
        cwd = Path(session["cwd"])
        if not cwd.is_absolute() or not cwd.is_dir() or cwd.resolve() != cwd:
            raise safe.SafeError("invalid")
        self.runner.require_preflight()
        env = self.runner.profile_env(target)
        # Preserve saved model, effort and permissions. No prompt, Auto flag,
        # permission bypass, settings transplant or cloud-history claim.
        # The signed Desktop does not honor CLAUDE_USER_DATA_DIR. The verified
        # already-running target was launched with --user-data-dir; the official
        # --desktop entry finds that instance. Registration is accepted later
        # only through its target-account metadata (or explicit UI verification).
        code = register_with_pty(self.runner.policy.cli, session["sessionId"], cwd, env)
        if code:
            raise safe.SafeError("failed")

    def verify_identity(self, target):
        # The overview can display a CLI identity before Desktop has logged in.
        # A switch receipt requires the stronger, matching Desktop identity.
        _, organization = self.runner.passive_metadata(target)
        if organization is None:
            raise safe.SafeError("identity_unavailable")

    def verify_sessions(self, target, sessions):
        identity_path = (self.runner.policy.home / ".claude.json" if target["kind"] == "current"
                         else Path(target["configDir"]) / ".claude.json")
        identity = safe.read_projected_json(identity_path, self.runner.policy,
            {"oauthAccount": {"accountUuid": True, "organizationUuid": True}}).get("oauthAccount", {})
        account = safe.canonical_uuid(identity.get("accountUuid"))
        organization = safe.canonical_uuid(identity.get("organizationUuid"))
        root = Path(target["desktopDir"]) / "claude-code-sessions"
        expected = {s["sessionId"]: s["cwd"] for s in sessions}
        matched = set()
        if not root.exists():
            return not expected
        for path in root.rglob("local_*.json"):
            if not {account, organization}.issubset(set(path.relative_to(root).parts[:-1])):
                continue
            data = safe.read_projected_json(path, self.runner.policy, {"cliSessionId": True, "cwd": True})
            ident = data.get("cliSessionId")
            if ident in expected:
                if data.get("cwd") != expected[ident]:
                    return False
                matched.add(ident)
        return matched == set(expected)


class SwitchManager:
    def __init__(self, registry, runner=None, vault=None, lifecycle=None):
        self.registry = registry
        self.runner = runner or safe.Runner(registry)
        self.policy = registry.policy
        self.vault = vault or vault_api.SessionVault(vault_api.VaultPolicy(self.policy.home, self.policy.uid))
        self.lifecycle = lifecycle or Lifecycle(self.runner)
        self.root = self.policy.root / "switches"

    def path(self, ident):
        return self.root / (safe.canonical_uuid(ident) + ".json")

    def save(self, op):
        op["updatedAt"] = safe.now()
        payload = json.dumps(op).encode()
        if len(payload) > safe.MAX_JSON:
            raise safe.SafeError("unsupported")
        safe.atomic_write(self.path(op["id"]), payload, self.policy)

    def read(self, ident):
        op = safe.read_json(self.path(ident), self.policy)
        if not isinstance(op, dict) or op.get("version") != 1 or op.get("id") != ident or op.get("stage") not in STAGES:
            raise safe.SafeError("invalid")
        self.registry.get(op["sourceId"], allow_archived=True); self.registry.get(op["targetId"], allow_archived=True)
        return op

    def public(self, op):
        keys = ("id", "sourceId", "targetId", "stage", "createdAt", "updatedAt", "handoffReceiptId", "sourceBackupId",
                "targetBackupId", "lastErrorCode", "verifiedAt", "verificationMode")
        return {**{key: op.get(key) for key in keys}, "sessionCount": len(op["sessions"]),
                "registeredCount": len(op["registered"]), "message": MESSAGES[op["stage"]],
                "rollbackAvailable": bool(op.get("handoffReceiptId")) and op["stage"] not in TERMINAL,
                "canCancel": not op.get("handoffReceiptId") and op["stage"] not in TERMINAL}

    def list(self):
        if not self.root.exists():
            return []
        safe.check_path(self.root, self.policy, private=True, directory=True)
        result = []
        for path in self.root.glob("*.json"):
            result.append(self.public(self.read(path.stem)))
        return sorted(result, key=lambda op: op["createdAt"], reverse=True)[:100]

    @contextlib.contextmanager
    def guard(self, source, target):
        with contextlib.ExitStack() as stack:
            for ident in sorted({safe.CURRENT_ID, source["id"], target["id"]}):
                stack.enter_context(self.runner.lock({"id": ident}))
            yield

    def prepare(self, source_id, target_id):
        source, target = self.registry.get(source_id), self.registry.get(target_id)
        if source_id == target_id:
            raise safe.SafeError("invalid")
        with self.guard(source, target):
            fresh = safe.Registry(str(self.policy.registry), self.policy)
            fresh.get(source_id); fresh.get(target_id)
            if any(op["stage"] not in TERMINAL for op in self.list()):
                raise safe.SafeError("locked")
            stored = safe.read_json(self.policy.registry, self.policy)
            op = {"version": 1, "id": str(uuid.uuid4()), "sourceId": source_id, "targetId": target_id,
                  "oldDefaultId": stored.get("selectedProfileId"), "stage": "planned", "createdAt": safe.now(),
                  "sessions": [], "registered": [], "inflight": None, "handoffReceiptId": None,
                  "sourceBackupId": None, "targetBackupId": None, "lastErrorCode": None, "verifiedAt": None, "verificationMode": None}
            self.save(op)
            return self.public(op)

    def set_archived(self, profile_id, archived):
        profile = self.registry.get(profile_id, allow_archived=True)
        with self.guard(profile, profile):
            fresh = safe.Registry(str(self.policy.registry), self.policy)
            profile = fresh.get(profile_id, allow_archived=True)
            stored = safe.read_json(self.policy.registry, self.policy)
            if profile["archived"] == archived:
                return {"ok": True, "code": None, "message": ""}
            if archived:
                if stored.get("selectedProfileId") == profile_id:
                    return {"ok": False, "code": "archive_selected", "message": ""}
                if self.root.exists():
                    safe.check_path(self.root, self.policy, private=True, directory=True)
                    for path in self.root.glob("*.json"):
                        operation = self.read(path.stem)
                        if operation["stage"] not in TERMINAL and profile_id in (
                                operation["sourceId"], operation["targetId"], operation.get("oldDefaultId")):
                            return {"ok": False, "code": "archive_switch_pending", "message": ""}
                state = self.vault.activity_probe(profile, self.vault.policy)
                if not state["known"] or state["busy"]:
                    return {"ok": False, "code": "archive_busy", "message": ""}
            # Only registry metadata changes. No profile, credential, snapshot,
            # health, receipt, backup or shared settings are removed or moved.
            row = next(p for p in stored["profiles"] if p["id"] == profile_id)
            row.update(archived=archived, archivedAt=safe.now() if archived else None, autoRefresh=False)
            safe.atomic_write(self.policy.registry, json.dumps(stored).encode(), self.policy)
            return {"ok": True, "code": None, "message": ""}

    def health_allows_target(self, target, *, allow_auth_check=False):
        health = self.runner.read_health(target)
        if health["status"] == "account_disabled":
            raise safe.SafeError("account_disabled")
        if health["status"] == "auth_required" and not allow_auth_check:
            raise safe.SafeError("signed_out")

    def check_target_auth(self, target, *, live=False):
        self.runner.require_preflight()
        env = self.runner.profile_env(target)
        try:
            auth = self.runner.auth_status(target, env)
            snapshot = self.runner.read_snapshot(target)
            previous_email = snapshot["auth"].get("email")
            identity_matches = bool(previous_email and auth.get("email") and previous_email.casefold() == auth["email"].casefold())
            if previous_email and not identity_matches:
                snapshot["usage"] = safe.empty_usage()
            if not identity_matches and self.runner.read_health(target)["status"] == "available":
                # A profile is not an identity binding. Never transfer another
                # login's recent availability evidence to this login.
                self.runner.save_health(target, safe.empty_health())
            snapshot["auth"] = auth
            self.runner.save_snapshot(target, snapshot)
            if auth["status"] != "signed_in":
                self.runner.observe_health(target, "auth_required", source="official_cli", reason="reauth_required")
                return False
            if live:
                # Explicit continuation after login, while both profiles are
                # idle: verify using the existing no-model official protocol.
                snapshot["usage"] = safe.normalize_usage(self.runner.query_usage(env))
                self.runner.save_snapshot(target, snapshot)
                self.runner.observe_health(target, "success")
            return True
        except safe.SafeError as error:
            if error.code in {"auth", "auth_expired", "signed_out", "account_disabled"}:
                disabled = error.code == "account_disabled"
                self.runner.observe_health(target, "account_disabled" if disabled else "auth_required", source="official_cli",
                                           reason="official_notice" if disabled else "auth_expired")
            raise

    def advance(self, op, source, target):
        self.health_allows_target(target, allow_auth_check=True)
        if op["stage"] == "copy_incomplete" and op.get("handoffReceiptId"):
            receipt, _ = self.vault._load_receipt(op["handoffReceiptId"])
            op["lastErrorCode"] = receipt.get("failureCode", "copy_interrupted")
            return
        if op["stage"] in TERMINAL | {"awaiting_verification", "registration_uncertain", "copy_incomplete", "rollback_waiting_exit"}:
            return
        if op["stage"] == "failed":
            op["stage"] = op.pop("resumeStage", "planned")
        state = self.lifecycle.runtime(source, target)
        if not state["known"] or state["other"]:
            raise safe.SafeError("busy")
        needs_auth = self.runner.read_health(target)["status"] == "auth_required"
        if needs_auth and op["stage"] not in {"planned", "waiting_exit", "waiting_login"}:
            if state["source"] or state["target"]:
                raise safe.SafeError("signed_out")
            if not self.check_target_auth(target, live=True):
                raise safe.SafeError("signed_out")
        if op["stage"] in {"planned", "waiting_exit", "waiting_login"}:
            if state["source"] or state["target"]:
                op["stage"] = "waiting_exit"
                return
            if not self.check_target_auth(target, live=needs_auth):
                op["stage"] = "waiting_login"
                return
            inventory = self.vault.inventory(source)
            if inventory["active"] or not inventory["activityKnown"] or any(
                    not row["stable"] or row["parseStatus"] != "valid" or not row["cwd"] for row in inventory["sessions"]):
                raise safe.SafeError("busy")
            op["sessions"] = [{"sessionId": row["sessionId"], "cwd": row["cwd"]} for row in inventory["sessions"]]
            maximum = {**op, "registered": [s["sessionId"] for s in op["sessions"]]}
            if len(json.dumps(maximum).encode()) > safe.MAX_JSON - 8192:
                op["sessions"] = []
                raise safe.SafeError("unsupported")
            op["stage"] = "copying"
            self.save(op)
            if op["sessions"]:
                def checkpoint(receipt):
                    op.update(handoffReceiptId=receipt["receiptId"], sourceBackupId=receipt["sourceBackupId"],
                              targetBackupId=receipt["targetBackupId"])
                    self.save(op)  # Durable pointer before any target publication.
                def backups(source_id, target_id):
                    op.update(sourceBackupId=source_id, targetBackupId=target_id)
                    self.save(op)
                result = self.vault.handoff(source, target, [s["sessionId"] for s in op["sessions"]],
                                            receipt_callback=checkpoint, backup_callback=backups)
                op["stage"] = "copied" if result["ok"] else "copy_incomplete"
                if not result["ok"]:
                    op["lastErrorCode"] = result.get("failureCode", "copy_interrupted")
            else:
                for key, profile in (("sourceBackupId", source), ("targetBackupId", target)):
                    op[key] = self.vault.snapshot(profile)["backupId"]
                    self.save(op)
                op["stage"] = "copied"
            self.save(op)
            return
        if op["stage"] == "copying":
            if not op["handoffReceiptId"]:
                op["stage"] = "copy_incomplete"  # Interrupted backup; never silently recopy.
            else:
                receipt, _ = self.vault._load_receipt(op["handoffReceiptId"])
                op["stage"] = "copied" if receipt["status"] == "copied" else "copy_incomplete"
                if op["stage"] == "copy_incomplete":
                    op["lastErrorCode"] = receipt.get("failureCode", "copy_interrupted")
            return
        if op["stage"] in {"copied", "launching", "waiting_desktop"}:
            if state["source"]:
                raise safe.SafeError("busy")
            if not state["targetDesktop"]:
                if state["target"]:
                    raise safe.SafeError("busy")
                if op["stage"] == "waiting_desktop":
                    return
                op["stage"] = "launching"; self.save(op)
                self.lifecycle.launch(target)
                op["stage"] = "waiting_desktop"
                return
            op["stage"] = "registering"
        if op["stage"] == "registering":
            if state["source"] or not state["targetDesktop"]:
                raise safe.SafeError("busy")
            if op.get("inflight"):
                op["stage"] = "registration_uncertain"
                return
            session = next((s for s in op["sessions"] if s["sessionId"] not in op["registered"]), None)
            if session:
                op["inflight"] = session["sessionId"]; self.save(op)
                try:
                    self.lifecycle.register(target, session)
                except Exception:
                    op["stage"] = "registration_uncertain"
                    return
                op["registered"].append(session["sessionId"])
                op["inflight"] = None
            if len(op["registered"]) == len(op["sessions"]):
                op["stage"] = "awaiting_verification"

    def act(self, ident, action, *, confirmed=False):
        op = self.read(ident)
        if op["stage"] in TERMINAL and action == "advance":
            return self.public(op)
        source, target = self.registry.get(op["sourceId"]), self.registry.get(op["targetId"])
        with self.guard(source, target):
            op = self.read(ident)
            fresh = safe.Registry(str(self.policy.registry), self.policy)
            source, target = fresh.get(op["sourceId"]), fresh.get(op["targetId"])
            try:
                op["lastErrorCode"] = None
                if action == "advance":
                    self.advance(op, source, target)
                elif action == "request_quit":
                    if not confirmed or op["stage"] not in {"planned", "waiting_exit", "rollback_waiting_exit"}:
                        raise safe.SafeError("invalid")
                    if op["stage"] != "rollback_waiting_exit":
                        self.health_allows_target(target, allow_auth_check=True)
                    self.lifecycle.quit(source, target, rollback=op["stage"] == "rollback_waiting_exit")
                    if op["stage"] != "rollback_waiting_exit":
                        op["stage"] = "waiting_exit"
                elif action in {"ack_registration", "retry_registration"}:
                    if not confirmed or op["stage"] != "registration_uncertain" or not op.get("inflight"):
                        raise safe.SafeError("invalid")
                    if action == "ack_registration":
                        op["registered"].append(op["inflight"])
                    op["inflight"] = None
                    op["stage"] = "registering"
                elif action == "retry_launch":
                    if not confirmed or op["stage"] != "waiting_desktop":
                        raise safe.SafeError("invalid")
                    state = self.lifecycle.runtime(source, target)
                    if not state["known"] or state["source"] or state["target"] or state["other"]:
                        raise safe.SafeError("busy")
                    op["stage"] = "copied"
                elif action in {"confirm", "verify_auto"}:
                    self.health_allows_target(target)
                    if (action == "confirm" and not confirmed) or op["stage"] not in {"awaiting_verification", "confirming"}:
                        raise safe.SafeError("invalid")
                    state = self.lifecycle.runtime(source, target)
                    if not state["known"] or state["source"] or state["other"] or not state["targetDesktop"]:
                        raise safe.SafeError("busy")
                    try:
                        self.lifecycle.verify_identity(target)
                    except safe.SafeError as error:
                        if action == "verify_auto" or error.code == "identity_mismatch":
                            raise
                    if action == "verify_auto":
                        health = self.runner.read_health(target)
                        if health["status"] != "available" or not health["observedAt"] or not 0 <= safe.now() - health["observedAt"] < 35 * 60:
                            self.save(op)
                            return self.public(op)
                        if not self.lifecycle.verify_sessions(target, op["sessions"]):
                            self.save(op)
                            return self.public(op)
                    op["verificationMode"] = "official_local_metadata" if action == "verify_auto" else "user_verified_ui"
                    op["stage"] = "confirming"; op["verifiedAt"] = safe.now(); self.save(op)
                    stored = safe.read_json(self.policy.registry, self.policy)
                    if stored.get("selectedProfileId") not in {op["oldDefaultId"], target["id"]}:
                        raise safe.SafeError("invalid")
                    stored["selectedProfileId"] = target["id"]
                    safe.atomic_write(self.policy.registry, json.dumps(stored).encode(), self.policy)
                    op["stage"] = "complete"
                elif action == "rollback":
                    if not confirmed or op["stage"] in TERMINAL or not op.get("handoffReceiptId"):
                        raise safe.SafeError("invalid")
                    state = self.lifecycle.runtime(source, target)
                    if not state["known"] or state["target"] or state["source"] or state["other"]:
                        op["stage"] = "rollback_waiting_exit"
                    else:
                        stored = safe.read_json(self.policy.registry, self.policy)
                        if stored.get("selectedProfileId") not in {op["oldDefaultId"], target["id"]}:
                            raise safe.SafeError("invalid")
                        self.vault.rollback(op["handoffReceiptId"])
                        if op.get("verifiedAt") and stored.get("selectedProfileId") == target["id"]:
                            stored["selectedProfileId"] = op["oldDefaultId"]
                            safe.atomic_write(self.policy.registry, json.dumps(stored).encode(), self.policy)
                        op["stage"] = "rolled_back"
                elif action == "cancel":
                    if op.get("handoffReceiptId") or op["stage"] in TERMINAL:
                        raise safe.SafeError("invalid")
                    if op.get("verifiedAt"):
                        stored = safe.read_json(self.policy.registry, self.policy)
                        if stored.get("selectedProfileId") not in {op["oldDefaultId"], target["id"]}:
                            raise safe.SafeError("invalid")
                        stored["selectedProfileId"] = op["oldDefaultId"]
                        safe.atomic_write(self.policy.registry, json.dumps(stored).encode(), self.policy)
                    op["stage"] = "cancelled"
                elif action == "abandon":
                    if not confirmed or op["stage"] in TERMINAL:
                        raise safe.SafeError("invalid")
                    op["stage"] = "abandoned"
                else:
                    raise safe.SafeError("invalid")
            except Exception as error:
                code = error.code if isinstance(error, (safe.SafeError, vault_api.VaultError)) else "failed"
                op["lastErrorCode"] = code if code in set(safe.SAFE_MESSAGES) | set(vault_api.MESSAGES) else "failed"
                if code in {"signed_out", "auth_expired", "auth"} and not op.get("handoffReceiptId"):
                    op["stage"] = "waiting_login"
                elif op["stage"] not in TERMINAL | {"failed", "copy_incomplete", "registration_uncertain", "rollback_waiting_exit", "awaiting_verification"}:
                    op["resumeStage"] = op["stage"]
                    op["stage"] = "failed"
            self.save(op)
            return self.public(op)


def main(argv=None):
    class SafeParser(argparse.ArgumentParser):
        def error(self, message):
            raise safe.SafeError("invalid")
    parser = SafeParser(add_help=False)
    parser.add_argument("action", choices=["list", "prepare", "advance", "request_quit", "confirm", "verify_auto", "rollback", "cancel", "ack_registration", "retry_registration", "retry_launch", "abandon", "archive_profile", "restore_profile"])
    parser.add_argument("--registry", required=True)
    parser.add_argument("--source-id"); parser.add_argument("--target-id"); parser.add_argument("--operation-id")
    parser.add_argument("--profile-id")
    parser.add_argument("--confirmed", action="store_true")
    try:
        args = parser.parse_args(argv)
        policy = safe.Policy.production()
        manager = SwitchManager(safe.Registry(args.registry, policy))
        if args.action == "list": result = manager.list()
        elif args.action == "prepare": result = manager.prepare(args.source_id, args.target_id)
        elif args.action in {"archive_profile", "restore_profile"}: result = manager.set_archived(args.profile_id, args.action == "archive_profile")
        else: result = manager.act(args.operation_id, args.action, confirmed=args.confirmed)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception:
        print(json.dumps({"ok": False, "message": "切换回执暂不可用；没有强制退出或自动切换账号。"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
