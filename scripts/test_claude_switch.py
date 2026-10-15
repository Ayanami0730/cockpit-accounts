"""Switch flow tests: synthetic profiles, real vault, fake official lifecycle."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import io
from contextlib import redirect_stdout
from unittest import mock
import uuid

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("switch_test", Path(__file__).with_name("claude-switch.py"))
switch = importlib.util.module_from_spec(spec); sys.modules[spec.name] = switch; spec.loader.exec_module(switch)
safe, vault_api = switch.safe, switch.vault_api


class FakeLifecycle:
    def __init__(self):
        self.active = set(); self.calls = []; self.refuse_quit = False; self.fail_registration = False
    def runtime(self, source, target):
        return {"source": source["id"] in self.active, "target": target["id"] in self.active,
                "sourceDesktop": source["id"] in self.active, "targetDesktop": target["id"] in self.active,
                "other": False, "known": True}
    def quit(self, source, target, *, rollback=False):
        self.calls.append("quit")
        if not self.refuse_quit: self.active.discard((target if rollback else source)["id"])
    def launch(self, target):
        self.calls.append("launch"); self.active.add(target["id"])
    def register(self, target, session):
        self.calls.append(("register", session["sessionId"]))
        if self.fail_registration: raise safe.SafeError("timeout")
    def verify_identity(self, target): self.calls.append("verify_local_identity")
    def verify_sessions(self, target, sessions): return True


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claude-switch-test-")
        self.home = Path(self.temp.name).resolve(); self.home.chmod(0o700)
        self.policy = safe.Policy(self.home, os.getuid(), "fixture")
        self.vpolicy = vault_api.VaultPolicy(self.home, os.getuid())
        self.source = self.profile(safe.CURRENT_ID, "current")
        self.target = self.profile(str(uuid.uuid4()), "managed")
        self.write(self.policy.registry, json.dumps({"version":1,"selectedProfileId":self.source["id"],"profiles":[self.source,self.target]}).encode())
        self.registry = safe.Registry(str(self.policy.registry), self.policy)
        self.runner = safe.Runner(self.registry)
        self.lifecycle = FakeLifecycle()
        self.vault = vault_api.SessionVault(self.vpolicy, lambda p, _: {"known":True,"busy":p["id"] in self.lifecycle.active,"sessionIds":set()})
        self.manager = switch.SwitchManager(self.registry, self.runner, self.vault, self.lifecycle)
        self.cwd = self.home / "project"; self.cwd.mkdir()
        self.sessions = [str(uuid.uuid4()), str(uuid.uuid4())]
        for sid in self.sessions:
            self.write(Path(self.source["configDir"]) / "projects/project" / (sid + ".jsonl"),
                json.dumps({"sessionId":sid,"cwd":str(self.cwd),"type":"user","message":"SYNTHETIC_PRIVATE_TEXT"}).encode() + b"\n")
        self.runner.require_preflight = mock.Mock()
        self.runner.profile_env = mock.Mock(return_value={"HOME":str(self.home)})
        self.runner.auth_status = mock.Mock(return_value={"status":"signed_in","email":"target@example.invalid","plan":"pro","checkedAt":1,"source":"official_cli"})
        self.runner.run_bounded = mock.Mock(side_effect=AssertionError("No real command may run"))
    def tearDown(self): self.temp.cleanup()
    def write(self, path, data):
        vault_api.private_mkdir(path.parent, self.vpolicy); path.write_bytes(data); path.chmod(0o600)
    def profile(self, ident, kind):
        cli = self.policy.current_cli if kind == "current" else self.policy.root / "profiles" / ident / "cli"
        desktop = self.policy.current_desktop if kind == "current" else self.policy.root / "profiles" / ident / "desktop"
        for p in (cli, desktop): vault_api.private_mkdir(p, self.vpolicy)
        return {"id":ident,"kind":kind,"label":kind,"configDir":str(cli),"desktopDir":str(desktop),"createdAt":1,
                "collectorInstalled":False,"autoRefresh":True,"renewalDate":None}
    def prepare(self): return self.manager.prepare(self.source["id"], self.target["id"])
    def drive(self, op):
        for _ in range(12):
            op = self.manager.act(op["id"], "advance")
            if op["stage"] in {"awaiting_verification","failed","waiting_exit","waiting_login","registration_uncertain","copy_incomplete"}: return op
        self.fail(op)
    def selected(self): return json.loads(self.policy.registry.read_text())["selectedProfileId"]

    def test_normal_switch_batches_once_and_changes_default_only_after_explicit_verification(self):
        op = self.prepare()
        with mock.patch.object(self.vault, "_snapshot", wraps=self.vault._snapshot) as backups:
            op = self.drive(op)
        self.assertEqual(op["stage"], "awaiting_verification")
        self.assertEqual(op["registeredCount"], 2)
        self.assertEqual(backups.call_count, 2)
        self.assertEqual(self.selected(), self.source["id"])
        self.assertEqual(self.manager.act(op["id"], "confirm")["stage"], "awaiting_verification")
        op = self.manager.act(op["id"], "confirm", confirmed=True)
        self.assertEqual(op["stage"], "complete")
        self.assertEqual(self.selected(), self.target["id"])
        self.assertNotIn("SYNTHETIC_PRIVATE_TEXT", json.dumps(self.manager.list()))

    def test_busy_task_waits_and_quit_is_never_implicit_or_forced(self):
        self.lifecycle.active.add(self.source["id"])
        op = self.manager.act(self.prepare()["id"], "advance")
        self.assertEqual(op["stage"], "waiting_exit")
        self.runner.auth_status.assert_not_called(); self.runner.require_preflight.assert_not_called()
        self.assertEqual(self.lifecycle.calls, [])
        self.lifecycle.refuse_quit = True
        op = self.manager.act(op["id"], "request_quit", confirmed=True)
        self.assertIn(self.source["id"], self.lifecycle.active)
        self.assertEqual(self.manager.act(op["id"], "advance")["stage"], "waiting_exit")
        self.assertFalse(self.vpolicy.backups.exists())

    def test_already_logged_in_does_not_launch_login_and_expired_login_has_clear_pause(self):
        self.runner.auth_status.return_value = {**safe.empty_auth(), "status":"signed_out", "source":"official_cli"}
        op = self.drive(self.prepare())
        self.assertEqual(op["stage"], "waiting_login")
        self.assertEqual(self.lifecycle.calls, [])
        self.assertFalse(self.vpolicy.backups.exists())

    def test_explicit_suspension_prevents_every_official_operation_but_can_preserve_source(self):
        self.runner.record_health(self.target, "account_disabled", "supported_countries_policy", "official_email", safe.now())
        op = self.drive(self.prepare())
        self.assertEqual(op["lastErrorCode"], "account_disabled")
        self.runner.auth_status.assert_not_called(); self.runner.require_preflight.assert_not_called()
        self.assertEqual(self.lifecycle.calls, [])

    def test_restart_does_not_duplicate_backups_or_registered_sessions(self):
        op = self.prepare()
        op = self.manager.act(op["id"], "advance")
        self.assertEqual(op["stage"], "copied")
        self.manager = switch.SwitchManager(self.registry, self.runner, self.vault, self.lifecycle)
        with mock.patch.object(self.vault, "handoff", side_effect=AssertionError("must not copy twice")):
            op = self.drive(op)
        self.assertEqual(op["stage"], "awaiting_verification")
        self.assertEqual(len([v for v in self.lifecycle.calls if isinstance(v, tuple)]), 2)

    def test_uncertain_registration_requires_explicit_choice_before_retry(self):
        self.lifecycle.fail_registration = True
        op = self.drive(self.prepare())
        self.assertEqual(op["stage"], "registration_uncertain")
        before = list(self.lifecycle.calls)
        self.manager.act(op["id"], "advance")
        self.assertEqual(self.lifecycle.calls, before)
        self.lifecycle.fail_registration = False
        op = self.manager.act(op["id"], "retry_registration", confirmed=True)
        self.assertEqual(self.drive(op)["stage"], "awaiting_verification")

    def test_handoff_pointer_persists_before_publication_and_can_roll_back_interrupted_copy(self):
        original = self.vault.handoff
        def interrupt(source, target, ids, receipt_callback=None, backup_callback=None):
            def saved(receipt):
                receipt_callback(receipt)
                raise RuntimeError("simulated crash")
            return original(source, target, ids, receipt_callback=saved, backup_callback=backup_callback)
        with mock.patch.object(self.vault, "handoff", side_effect=interrupt):
            op = self.drive(self.prepare())
        self.assertTrue(op["handoffReceiptId"])
        op = self.manager.act(op["id"], "advance")
        self.assertEqual(op["stage"], "copy_incomplete")
        self.assertEqual(self.manager.act(op["id"], "rollback", confirmed=True)["stage"], "rolled_back")

    def test_rollback_refuses_target_that_was_used_after_copy(self):
        op = self.manager.act(self.prepare()["id"], "advance")
        target = Path(self.target["configDir"]) / "projects/project" / (self.sessions[0] + ".jsonl")
        with target.open("ab") as stream: stream.write(b'{"new":"work"}\n')
        op = self.manager.act(op["id"], "rollback", confirmed=True)
        self.assertNotEqual(op["stage"], "rolled_back")
        self.assertTrue(target.read_bytes().endswith(b'{"new":"work"}\n'))

    def test_return_switch_accepts_strict_history_prefix_and_refuses_divergence(self):
        op = self.drive(self.prepare()); self.manager.act(op["id"], "confirm", confirmed=True)
        self.lifecycle.active.clear()
        target = Path(self.target["configDir"]) / "projects/project" / (self.sessions[0] + ".jsonl")
        with target.open("ab") as stream: stream.write(b'{"type":"assistant","new":"saved work"}\n')
        reverse = self.manager.prepare(self.target["id"], self.source["id"])
        reverse = self.manager.act(reverse["id"], "advance")
        self.assertEqual(reverse["stage"], "copied")
        original = Path(self.source["configDir"]) / "projects/project" / target.name
        self.assertEqual(original.read_bytes(), target.read_bytes())

    def test_recent_official_health_and_matching_metadata_allow_automatic_completion(self):
        self.runner.save_snapshot(self.target, {"auth":self.runner.auth_status.return_value,"usage":safe.empty_usage()})
        self.runner.record_health(self.target, "available", "usage_page_available", "official_ui", safe.now())
        op = self.drive(self.prepare())
        op = self.manager.act(op["id"], "verify_auto")
        self.assertEqual(op["stage"], "complete")
        self.assertEqual(op["verificationMode"], "official_local_metadata")

    def test_different_login_cannot_borrow_recent_health_from_previous_account(self):
        self.runner.save_snapshot(self.target, {"auth":{**self.runner.auth_status.return_value,"email":"old@example.invalid"},"usage":safe.empty_usage()})
        self.runner.record_health(self.target, "available", "usage_page_available", "official_ui", safe.now())
        op = self.drive(self.prepare())
        self.assertEqual(self.manager.act(op["id"], "verify_auto")["stage"], "awaiting_verification")
        self.assertEqual(self.runner.read_health(self.target)["status"], "unknown")

    def test_stale_health_never_automatically_completes_switch(self):
        self.runner.record_health(self.target, "available", "usage_page_available", "official_ui", safe.now() - 3600)
        op = self.drive(self.prepare())
        self.assertEqual(self.manager.act(op["id"], "verify_auto")["stage"], "awaiting_verification")
        self.assertEqual(self.selected(), self.source["id"])

    def test_reauth_after_official_login_uses_one_explicit_idle_query(self):
        self.runner.record_health(self.target, "auth_required", "reauth_required", "official_ui", safe.now())
        self.runner.query_usage = mock.Mock(return_value={"rate_limits_available":True,"rate_limits":{"five_hour":{"utilization":1}}})
        op = self.drive(self.prepare())
        self.assertEqual(op["stage"], "awaiting_verification")
        self.runner.query_usage.assert_called_once()
        self.assertEqual(self.runner.read_health(self.target)["status"], "available")

    def test_waiting_desktop_can_be_explicitly_relaunched_and_reused(self):
        op = self.manager.act(self.prepare()["id"], "advance")
        op = self.manager.act(op["id"], "advance")
        self.assertEqual(op["stage"], "waiting_desktop")
        self.lifecycle.active.clear()
        op = self.manager.act(op["id"], "retry_launch", confirmed=True)
        self.assertEqual(self.drive(op)["stage"], "awaiting_verification")

    def test_confirm_crash_then_rollback_restores_default_and_is_idempotent(self):
        op = self.drive(self.prepare()); self.lifecycle.active.clear()
        saved = self.manager.read(op["id"]); saved.update(stage="confirming", verifiedAt=safe.now()); self.manager.save(saved)
        registry = json.loads(self.policy.registry.read_text()); registry["selectedProfileId"] = self.target["id"]
        self.write(self.policy.registry, json.dumps(registry).encode())
        self.vault.rollback(op["handoffReceiptId"])  # Simulate crash before manager checkpoint.
        op = self.manager.act(op["id"], "rollback", confirmed=True)
        self.assertEqual(op["stage"], "rolled_back")
        self.assertEqual(self.selected(), self.source["id"])

    def test_zero_history_cancel_restores_default_after_confirmation_crash(self):
        for sid in self.sessions:
            (Path(self.source["configDir"]) / "projects/project" / (sid + ".jsonl")).unlink()
        op = self.drive(self.prepare())
        self.assertIsNone(op["handoffReceiptId"])
        saved = self.manager.read(op["id"]); saved.update(stage="confirming", verifiedAt=safe.now()); self.manager.save(saved)
        registry = json.loads(self.policy.registry.read_text()); registry["selectedProfileId"] = self.target["id"]
        self.write(self.policy.registry, json.dumps(registry).encode())
        self.assertEqual(self.manager.act(op["id"], "cancel")["stage"], "cancelled")
        self.assertEqual(self.selected(), self.source["id"])

    def test_changed_files_can_be_kept_when_rollback_is_not_allowed(self):
        op = self.drive(self.prepare())
        self.runner.record_health(self.target, "account_disabled", "official_notice", "official_email", safe.now())
        op = self.manager.act(op["id"], "abandon", confirmed=True)
        self.assertEqual(op["stage"], "abandoned")
        self.assertTrue((Path(self.target["configDir"]) / "projects/project" / (self.sessions[0] + ".jsonl")).exists())
        self.manager.prepare(self.target["id"], self.source["id"])

    def test_oversized_plan_is_rejected_before_any_handoff_and_receipt_remains_readable(self):
        rows = [{"sessionId":str(uuid.uuid4()),"cwd":"/"+"a"*1000,"stable":True,"parseStatus":"valid"} for _ in range(500)]
        with mock.patch.object(self.vault, "inventory", return_value={"active":False,"activityKnown":True,"sessions":rows}), \
             mock.patch.object(self.vault, "handoff") as handoff:
            op = self.drive(self.prepare())
        self.assertEqual(op["stage"], "failed")
        handoff.assert_not_called()
        self.assertTrue(self.manager.list())

    def test_registration_adapter_requires_target_instance_but_allows_its_code_processes(self):
        adapter = switch.Lifecycle(self.runner)
        self.runner.processes = mock.Mock(return_value=[{"pid":101,"desktop":True}, {"pid":102,"desktop":False}])
        meta = {"configDir":self.target["configDir"],"desktopDir":self.target["desktopDir"],"sessionIds":set()}
        with mock.patch.object(vault_api, "proc_allowlist", return_value=meta):
            state = adapter.runtime(self.source, self.target)
        self.assertTrue(state["targetDesktop"]); self.assertFalse(state["source"]); self.assertFalse(state["other"])
        with mock.patch.object(switch, "register_with_pty", return_value=0) as pty_run:
            adapter.register(self.target, {"sessionId":self.sessions[0],"cwd":str(self.cwd)})
        self.runner.profile_env.assert_called_with(self.target)
        self.assertEqual(pty_run.call_args.args[:3], (self.policy.cli, self.sessions[0], self.cwd))
        self.assertNotIn("CLAUDE_USER_DATA_DIR", pty_run.call_args.args[3])

    def test_zero_history_auto_verification_requires_desktop_login(self):
        for sid in self.sessions:
            (Path(self.source["configDir"]) / "projects/project" / (sid + ".jsonl")).unlink()
        account, organization = str(uuid.uuid4()), str(uuid.uuid4())
        self.write(Path(self.target["configDir"]) / ".claude.json", json.dumps({"oauthAccount": {
            "accountUuid": account, "organizationUuid": organization, "emailAddress": "target@example.invalid"
        }}).encode())
        op = self.drive(self.prepare())
        self.runner.observe_health(self.target, "success")
        adapter = switch.Lifecycle(self.runner)
        with mock.patch.object(self.lifecycle, "verify_identity", side_effect=adapter.verify_identity), \
             mock.patch.object(self.lifecycle, "verify_sessions", side_effect=adapter.verify_sessions):
            op = self.manager.act(op["id"], "verify_auto")
            self.assertEqual(op["stage"], "awaiting_verification")
            self.assertEqual(op["lastErrorCode"], "identity_unavailable")
            self.assertIsNone(op["verifiedAt"])
            self.assertEqual(self.selected(), self.source["id"])
            self.write(Path(self.target["desktopDir"]) / "config.json",
                       json.dumps({"lastKnownAccountUuid": account}).encode())
            op = self.manager.act(op["id"], "verify_auto")
        self.assertEqual(op["stage"], "complete")
        self.assertEqual(op["verificationMode"], "official_local_metadata")
        self.assertEqual(self.selected(), self.target["id"])
        self.runner.run_bounded.assert_not_called()

    def test_desktop_identity_mismatch_still_rejects_verification(self):
        self.write(Path(self.target["configDir"]) / ".claude.json", json.dumps({"oauthAccount": {
            "accountUuid": str(uuid.uuid4()), "organizationUuid": str(uuid.uuid4()),
            "emailAddress": "target@example.invalid"
        }}).encode())
        self.write(Path(self.target["desktopDir"]) / "config.json",
                   json.dumps({"lastKnownAccountUuid": str(uuid.uuid4())}).encode())
        with self.assertRaises(safe.SafeError) as error:
            switch.Lifecycle(self.runner).verify_identity(self.target)
        self.assertEqual(error.exception.code, "identity_mismatch")
        self.runner.run_bounded.assert_not_called()

    def test_standard_profile_session_verification_uses_home_global_metadata(self):
        account, organization = str(uuid.uuid4()), str(uuid.uuid4())
        self.write(self.home / ".claude.json", json.dumps({"oauthAccount": {
            "accountUuid": account, "organizationUuid": organization, "emailAddress": "source@example.invalid"
        }}).encode())
        # This is the custom-config location, not the standard CLI's global file.
        self.write(Path(self.source["configDir"]) / ".claude.json", json.dumps({"oauthAccount": {
            "accountUuid": str(uuid.uuid4()), "organizationUuid": str(uuid.uuid4())
        }}).encode())
        self.write(Path(self.source["desktopDir"]) / "config.json",
                   json.dumps({"lastKnownAccountUuid": account}).encode())
        root = Path(self.source["desktopDir"]) / "claude-code-sessions" / account / organization
        for sid in self.sessions:
            self.write(root / ("local_" + sid + ".json"),
                       json.dumps({"cliSessionId": sid, "cwd": str(self.cwd)}).encode())
        adapter = switch.Lifecycle(self.runner)
        adapter.verify_identity(self.source)
        self.assertTrue(adapter.verify_sessions(self.source,
            [{"sessionId": sid, "cwd": str(self.cwd)} for sid in self.sessions]))
        self.runner.run_bounded.assert_not_called()

    def test_registration_frontend_has_real_tty_streams_and_never_echoes_output(self):
        cli = self.home / "fake-tty-cli"
        cli.write_text("#!" + sys.executable + "\nimport os,sys\nassert os.isatty(0) and os.isatty(1) and os.isatty(2)\nassert sys.argv[1:3] == ['--desktop','--resume']\nprint('SECRET_TTY_OUTPUT')\n")
        cli.chmod(0o700)
        output = io.StringIO()
        with redirect_stdout(output):
            code = switch.register_with_pty(cli, self.sessions[0], self.cwd, {"HOME":str(self.home)}, timeout=10)
        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue(), "")

    def test_registration_timeout_terminates_only_its_owned_frontend(self):
        cli = self.home / "fake-slow-cli"
        cli.write_text("#!" + sys.executable + "\nimport time\ntime.sleep(10)\n"); cli.chmod(0o700)
        with mock.patch.object(switch.os, "killpg", side_effect=AssertionError("must not kill process groups")), self.assertRaises(safe.SafeError) as caught:
            switch.register_with_pty(cli, self.sessions[0], self.cwd, {"HOME":str(self.home)}, timeout=0.1)
        self.assertEqual(caught.exception.code, "timeout")

    def test_unclassified_process_during_reuse_stops_copy_and_zero_write_rollback_is_safe(self):
        self.vault.handoff(self.source, self.target, self.sessions)
        files, _ = self.vault._files(self.target)
        before = {str(row["path"]): vault_api.file_state(row["path"].stat()) for row in files}
        reopened = {"unknown": False}
        self.vault.activity_probe = lambda *_: {"known":not reopened["unknown"],"busy":reopened["unknown"],"sessionIds":set()}
        original = vault_api.atomic_json
        def reopen_after_reuse(path, value, policy):
            original(path, value, policy)
            if value.get("kind") == "handoff" and value.get("status") == "prepared" and len(value.get("files", [])) == 1:
                reopened["unknown"] = True
        with mock.patch.object(vault_api, "atomic_json", side_effect=reopen_after_reuse):
            op = self.drive(self.prepare())
        self.assertEqual(op["stage"], "copy_incomplete")
        self.assertEqual(op["lastErrorCode"], "active")
        receipt, _ = self.vault._load_receipt(op["handoffReceiptId"])
        self.assertEqual(receipt["failureCode"], "active")
        self.assertTrue(all(row["operation"] == "reused" for row in receipt["files"]))
        self.assertEqual(self.manager.act(op["id"], "advance")["lastErrorCode"], "active")
        reopened["unknown"] = False
        op = self.manager.act(op["id"], "rollback", confirmed=True)
        self.assertEqual(op["stage"], "rolled_back")
        self.assertEqual(before, {str(row["path"]): vault_api.file_state(row["path"].stat()) for row in files})

    def test_old_partial_receipt_never_invents_the_missing_failure_reason(self):
        op = self.manager.act(self.prepare()["id"], "advance")
        receipt, path = self.vault._load_receipt(op["handoffReceiptId"])
        receipt["status"] = "partial"; receipt.pop("failureCode", None)
        vault_api.atomic_json(path, receipt, self.vpolicy)
        stored = self.manager.read(op["id"]); stored["stage"] = "copy_incomplete"; self.manager.save(stored)
        self.assertEqual(self.manager.act(op["id"], "advance")["lastErrorCode"], "copy_interrupted")

    def test_archive_preserves_other_working_profile_and_every_profile_file(self):
        self.lifecycle.active.add(self.source["id"])
        secret = Path(self.target["configDir"]) / ".credentials.json"
        self.write(secret, b"SYNTHETIC_CREDENTIAL_MUST_NOT_BE_READ")
        before_file = vault_api.file_state(secret.stat())
        before_registry = json.loads(self.policy.registry.read_text())
        with mock.patch.object(self.runner, "auth_status") as auth, mock.patch.object(self.runner, "query_usage") as query:
            result = self.manager.set_archived(self.target["id"], True)
        self.assertTrue(result["ok"])
        auth.assert_not_called(); query.assert_not_called()
        after = json.loads(self.policy.registry.read_text())
        self.assertEqual(after["selectedProfileId"], self.source["id"])
        self.assertEqual(after["profiles"][0], before_registry["profiles"][0])
        self.assertTrue(after["profiles"][1]["archived"])
        self.assertFalse(after["profiles"][1]["autoRefresh"])
        self.assertEqual(vault_api.file_state(secret.stat()), before_file)
        self.assertEqual(secret.read_bytes(), b"SYNTHETIC_CREDENTIAL_MUST_NOT_BE_READ")

    def test_archive_rejects_selected_owned_or_unclassified_process(self):
        before = self.policy.registry.read_bytes()
        self.assertEqual(self.manager.set_archived(self.source["id"], True)["code"], "archive_selected")
        self.lifecycle.active.add(self.target["id"])
        self.assertEqual(self.manager.set_archived(self.target["id"], True)["code"], "archive_busy")
        self.lifecycle.active.clear()
        with mock.patch.object(self.vault, "activity_probe", return_value={"known":False,"busy":False}):
            self.assertEqual(self.manager.set_archived(self.target["id"], True)["code"], "archive_busy")
        self.assertEqual(self.policy.registry.read_bytes(), before)

    def test_archive_rejects_pending_switch_source_target_and_old_default(self):
        op = self.prepare()
        self.assertEqual(self.manager.set_archived(self.target["id"], True)["code"], "archive_switch_pending")
        self.manager.act(op["id"], "cancel")
        op = self.manager.prepare(self.target["id"], self.source["id"])
        self.assertEqual(self.manager.set_archived(self.target["id"], True)["code"], "archive_switch_pending")
        self.manager.act(op["id"], "cancel")
        third = self.profile(str(uuid.uuid4()), "managed")
        stored = json.loads(self.policy.registry.read_text()); stored["profiles"].append(third); stored["selectedProfileId"] = self.target["id"]
        self.write(self.policy.registry, json.dumps(stored).encode())
        self.manager = switch.SwitchManager(safe.Registry(str(self.policy.registry),self.policy), self.runner, self.vault, self.lifecycle)
        self.manager.prepare(self.source["id"], third["id"])
        stored["selectedProfileId"] = self.source["id"]; self.write(self.policy.registry, json.dumps(stored).encode())
        self.assertEqual(self.manager.set_archived(self.target["id"], True)["code"], "archive_switch_pending")

    def test_archived_profile_keeps_history_readable_and_restore_changes_no_default_or_health(self):
        op = self.manager.prepare(self.target["id"], self.source["id"])
        op = self.drive(op); self.manager.act(op["id"], "confirm", confirmed=True)
        self.runner.record_health(self.target, "account_disabled", "official_notice", "official_email", safe.now())
        health = self.runner.health_path(self.target).read_bytes()
        self.assertTrue(self.manager.set_archived(self.target["id"], True)["ok"])
        registry = safe.Registry(str(self.policy.registry),self.policy)
        with self.assertRaises(safe.SafeError): registry.get(self.target["id"])
        restored_manager = switch.SwitchManager(registry, self.runner, self.vault, self.lifecycle)
        self.assertEqual(restored_manager.list()[0]["stage"], "complete")
        self.assertEqual(restored_manager.act(op["id"], "advance")["stage"], "complete")
        self.assertTrue(restored_manager.set_archived(self.target["id"], False)["ok"])
        self.assertEqual(self.selected(), self.source["id"])
        self.assertEqual(self.runner.health_path(self.target).read_bytes(), health)
        restored = safe.Registry(str(self.policy.registry),self.policy).get(self.target["id"])
        self.assertFalse(restored["autoRefresh"])

    def test_archived_stale_launch_and_query_inputs_are_rechecked_before_any_side_effect(self):
        self.assertTrue(self.manager.set_archived(self.target["id"], True)["ok"])
        actions = [lambda:self.runner.refresh(self.target), lambda:self.runner.terminal(self.target,"launch_cli"),
                   lambda:self.runner.terminal_child(self.target,"launch_cli"), lambda:self.runner.launch_desktop(self.target),
                   lambda:self.runner.inspect_auth(self.target), lambda:self.runner.install_collector(self.target),
                   lambda:self.runner.collect(self.target,b'{"rate_limits":{"five_hour":{"used_percentage":1}}}'),
                   lambda:self.runner.record_health(self.target,"auth_required","reauth_required","official_ui",safe.now())]
        with mock.patch.object(safe.os,"execve") as execute:
            for action in actions:
                with self.assertRaises(safe.SafeError) as error: action()
                self.assertEqual(error.exception.code,"archived")
        execute.assert_not_called(); self.runner.run_bounded.assert_not_called(); self.runner.auth_status.assert_not_called()
        self.runner.require_preflight.assert_not_called()
        with self.assertRaises(safe.SafeError): self.manager.prepare(self.source["id"], self.target["id"])

    def test_archive_revalidates_paths_and_refuses_symlink_escape_without_write(self):
        target = Path(self.target["configDir"]); target.rmdir()
        outside = self.home / "outside"; outside.mkdir(); target.symlink_to(outside, target_is_directory=True)
        before = self.policy.registry.read_bytes()
        with self.assertRaises(safe.SafeError): self.manager.set_archived(self.target["id"],True)
        self.assertEqual(self.policy.registry.read_bytes(),before)
        self.assertEqual(list(outside.iterdir()),[])

    def test_launch_parent_releases_profile_lock_before_starting_guarded_child(self):
        def open_terminal(args, **kwargs):
            self.assertEqual(args[:3],["/usr/bin/open","-a","Terminal"])
            with self.runner.lock(self.target): pass
            return 0, b""
        self.runner.run_bounded = mock.Mock(side_effect=open_terminal)
        self.assertTrue(self.runner.terminal(self.target,"launch_cli")["ok"])


if __name__ == "__main__": unittest.main()
