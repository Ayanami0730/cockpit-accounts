#!/usr/bin/env python3
"""Fixed local IPC: safe account summaries, session archive and official CLI resume.

This module never handles OAuth credentials. HTTP consumers are limited to the
two read-only actions by claude-view-middleware.mjs; native IPC owns mutations.
"""
import argparse
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import sys


def load_sibling(name, filename):
    path = Path(__file__).resolve().with_name(filename)
    stat = path.lstat()
    if path.is_symlink() or not path.is_file() or stat.st_uid not in (0, os.getuid()) or stat.st_mode & 0o022:
        raise RuntimeError("Unavailable local helper")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


safe = load_sibling("claude_companion_runner", "claude-local-safe-runner.py")


def session_vault():
    return load_sibling("claude_companion_vault", "claude-session-vault.py").SessionVault()


def session_inventory(profile):
    vault = session_vault()
    result = vault.inventory(profile)
    result["lastBackup"] = None
    root = vault.policy.backups
    if root.exists():
        candidates = sorted((p.name for p in root.iterdir() if re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f-]{36}", p.name)), reverse=True)
        for ident in candidates[:512]:
            try:
                receipt, path = vault._load_receipt(ident)
                if receipt.get("kind") != "snapshot" or receipt.get("profile", {}).get("id") != profile["id"]:
                    continue
                summary = receipt["summary"]
                result["lastBackup"] = {"ok": True, "backupId": ident, "backupPath": str(path.parent),
                    "receiptPath": str(path), **{key: summary[key] for key in (
                        "files", "bytes", "unstableFiles", "incompleteFiles", "invalidFiles")}}
                break
            except Exception:
                continue
    return result


def overview(policy):
    if not policy.registry.exists():
        return {"profiles": [], "selectedProfileId": None, "desktopRunning": False,
                "codeProcessCount": 0, "checkedAt": safe.now(), "warning": None}
    registry = safe.Registry(str(policy.registry), policy)
    runner = safe.Runner(registry)
    stored = safe.read_json(policy.registry, policy)
    selected = stored.get("selectedProfileId")
    profiles, archived = [], []
    for profile in registry.profiles.values():
        if profile["archived"]:
            archived.append({key: profile[key] for key in ("id", "label", "kind", "archivedAt")})
            continue
        try:
            snapshot = runner.cached_snapshot(profile)
        except (safe.SafeError, OSError, ValueError, TypeError):
            # One unreadable snapshot must not hide the other profiles. Return
            # a fixed, credential-free row rather than any exception contents.
            snapshot = {"auth": {**safe.empty_auth(), "status": "error"},
                        "usage": {**safe.empty_usage(safe.SAFE_MESSAGES["invalid"]), "errorCode": "invalid_profile"}}
        # Deliberate allowlist: do not let new registry fields reach the browser.
        row = {key: profile[key] for key in ("id", "label", "kind", "configDir", "desktopDir",
               "createdAt", "collectorInstalled", "autoRefresh", "renewalDate")}
        try:
            health = runner.read_health(profile)
            previous = runner.read_snapshot(profile)["auth"]
            if health["status"] == "available" and (snapshot["usage"].get("errorCode") == "identity_mismatch" or
                    (previous.get("email") and previous.get("email") != snapshot["auth"].get("email"))):
                health = safe.empty_health()
        except (safe.SafeError, OSError, ValueError, TypeError):
            health = {**safe.empty_health(), "status": "unavailable"}
        row.update(selected=profile["id"] == selected, auth=snapshot["auth"], usage=snapshot["usage"], health=health)
        profiles.append(row)
    try:
        processes = runner.processes()
        desktop = any(row["desktop"] for row in processes)
        code = sum(bool(row["code"]) for row in processes)
        warning = None
    except Exception:
        desktop, code, warning = True, 0, "无法核对进程状态；暂不允许会话续接。"
    return {"profiles": profiles, "archivedProfiles": archived, "selectedProfileId": selected, "desktopRunning": desktop,
            "codeProcessCount": code, "checkedAt": safe.now(), "warning": warning}


def resumable(vault, profile, session_id):
    safe.canonical_uuid(session_id)
    plan = vault.safe_resume_plan(profile, session_id)
    if not plan.get("ok") or plan.get("args") != ["--resume", session_id]:
        raise safe.SafeError("invalid")
    cwd = Path(plan["cwd"])
    if not cwd.is_absolute() or not cwd.is_dir() or "\x00" in str(cwd) or cwd.resolve() != cwd:
        raise safe.SafeError("invalid")
    return cwd


def resume(runner, source, target, session_id, *, terminal_child=False):
    vault = session_vault()
    runner.require_active(source); runner.require_active(target)
    if runner.read_health(target)["status"] == "account_disabled":
        raise safe.SafeError("account_disabled")
    # Recheck in the eventual Terminal process as well as when the button is hit.
    # No quit, signal, global auth swap or live transcript replacement is used.
    if runner.busy(source) or runner.busy(target):
        raise safe.SafeError("busy")
    profile = target if terminal_child else source
    cwd = resumable(vault, profile, session_id)
    target_inventory = vault.inventory(target)
    if target_inventory.get("active") or not target_inventory.get("activityKnown"):
        raise safe.SafeError("busy")
    runner.require_preflight()
    env = runner.profile_env(target)
    if terminal_child:
        cwd = resumable(vault, target, session_id)
        if runner.busy(source) or runner.busy(target):
            raise safe.SafeError("busy")
        os.chdir(cwd)
        os.execve(str(runner.policy.cli), [str(runner.policy.cli), "--resume", session_id], env)
    auth = runner.auth_status(target, env)
    if auth["status"] != "signed_in":
        raise safe.SafeError("signed_out")
    # A verified archive is required before any cross-profile history copy.
    if source["id"] != target["id"]:
        result = vault.handoff(source, target, [session_id])
        if not result.get("ok"):
            raise safe.SafeError("invalid")
    else:
        backup = vault.snapshot(source)
        if not backup.get("ok") or any(backup.get(key) for key in (
                "unstableFiles", "invalidFiles", "incompleteFiles", "unverifiedFiles", "symlinksSkipped")):
            raise safe.SafeError("invalid")
    runtime = runner.policy.root / "launchers"
    safe.ensure_private_dir(runtime, runner.policy)
    launcher = runtime / (target["id"] + "-resume-" + session_id + ".command")
    args = [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "resume-child", "--registry",
            str(runner.policy.registry), "--profile-id", source["id"], "--target-profile-id", target["id"],
            "--session-id", session_id]
    safe.atomic_write(launcher, ("#!/bin/sh\nexec " + shlex.join(args) + "\n").encode(), runner.policy, mode=0o700)
    code, _ = runner.run_bounded(["/usr/bin/open", "-a", "Terminal", str(launcher)], timeout=8)
    if code:
        raise safe.SafeError("terminal_failed")
    return {"ok": True, "message": "已请求通过官方 CLI 恢复会话。"}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["overview", "sessions", "snapshot", "resume", "resume-child"])
    parser.add_argument("--registry", required=True)
    parser.add_argument("--profile-id")
    parser.add_argument("--target-profile-id")
    parser.add_argument("--session-id")
    args = parser.parse_args(argv)
    policy = safe.Policy.production()
    try:
        if args.registry != str(policy.registry):
            raise safe.SafeError("invalid")
        if args.action == "overview":
            result = overview(policy)
        else:
            registry = safe.Registry(args.registry, policy)
            source = registry.get(args.profile_id)
            runner = safe.Runner(registry)
            if args.action == "sessions":
                with runner.lock(source):
                    runner.require_active(source)
                    result = session_inventory(source)
            elif args.action == "snapshot":
                with runner.lock(source):
                    runner.require_active(source)
                    result = session_vault().snapshot(source)
            else:
                target = registry.get(args.target_profile_id)
                # Deterministic lock order prevents concurrent handoffs into a
                # target while allowing ordinary official Claude processes.
                with contextlib.ExitStack() as stack:
                    for ident in sorted({source["id"], target["id"]}):
                        stack.enter_context(runner.lock(registry.get(ident)))
                    result = resume(runner, source, target, args.session_id,
                                    terminal_child=args.action == "resume-child")
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception as error:
        # Never include raw exceptions, subprocess output, session text or paths
        # in error responses. Details stay out of browser and Tauri logs.
        code = "busy" if isinstance(error, safe.SafeError) and error.code == "busy" else "session_unavailable"
        result = {"ok": False, "code": code, "message": "操作未完成；源记录与已有备份保留，目标可能已有写入，请核对回执后继续。"}
        print(json.dumps(result, ensure_ascii=False))
        return 0 if args.action.startswith("resume") else 1


if __name__ == "__main__":
    raise SystemExit(main())
