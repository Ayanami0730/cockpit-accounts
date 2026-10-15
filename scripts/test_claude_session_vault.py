"""Synthetic session fixtures only; no real profile, app or network access."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock
import uuid

spec = importlib.util.spec_from_file_location("claude_session_vault", Path(__file__).with_name("claude-session-vault.py"))
vault = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = vault
spec.loader.exec_module(vault)


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claude-session-vault-test-")
        self.home = Path(self.temp.name)
        self.policy = vault.VaultPolicy(self.home, os.getuid())
        self.source = {"id": vault.CURRENT_ID, "kind": "current", "configDir": str(self.policy.current_cli), "desktopDir": str(self.policy.current_desktop)}
        ident = str(uuid.uuid4())
        base = self.policy.native / "profiles" / ident
        self.target = {"id": ident, "kind": "managed", "configDir": str(base / "cli"), "desktopDir": str(base / "desktop")}
        for profile in (self.source, self.target):
            for key in ("configDir", "desktopDir"):
                vault.private_mkdir(Path(profile[key]), self.policy)
        self.session_id = str(uuid.uuid4())
        self.project = "-temporary-project"
        self.relative = Path("projects") / self.project / (self.session_id + ".jsonl")
        self.session = Path(self.source["configDir"]) / self.relative
        self.cwd = self.home / "work"
        self.row = {"type": "user", "sessionId": self.session_id, "cwd": str(self.cwd), "message": {"content": "TEST_PROMPT_SECRET_NOT_A_TITLE"}}
        self.write(self.session, json.dumps(self.row).encode() + b"\n")
        root = self.session.parent / self.session_id
        self.write(root / "custom-title.json", b'{"customTitle":"Original research session"}')
        self.write(root / "subagents/agent-test.jsonl", json.dumps({"sessionId": str(uuid.uuid4()), "cwd": str(self.cwd)}).encode() + b"\n")
        self.write(root / "tool-results/result.txt", b"TEST_TOOL_ARTIFACT")
        self.write(Path(self.source["configDir"]) / "file-history" / self.session_id / "v1", b"TEST_CHECKPOINT")
        self.write(Path(self.source["configDir"]) / ".credentials.json", b"TEST_AUTH_NEVER_COPIED")
        self.write(Path(self.source["configDir"]) / ".claude.json", b"TEST_DEVICE_NEVER_COPIED")
        self.write(root / "config.json", b"TEST_CONFIG_NEVER_COPIED")
        desktop_index = Path(self.source["desktopDir"]) / "claude-code-sessions" / str(uuid.uuid4()) / str(uuid.uuid4()) / ("local_" + self.session_id + ".json")
        self.write(desktop_index, json.dumps({"cliSessionId": self.session_id, "title": "Desktop title", "cwd": str(self.cwd)}).encode())
        self.write(Path(self.source["desktopDir"]) / "Cookies", b"TEST_COOKIE_NEVER_COPIED")
        self.activity = lambda profile, policy: {"known": True, "busy": False, "sessionIds": set()}
        self.vault = vault.SessionVault(self.policy, self.activity)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, raw):
        vault.private_mkdir(path.parent, self.policy)
        path.write_bytes(raw)
        path.chmod(0o600)

    def test_npm_node_process_is_part_of_session_busy_guard(self):
        result = mock.Mock(returncode=0, stdout=f"{self.policy.uid} 1234 /usr/local/bin/node\n".encode())
        metadata = {"configDir": self.source["configDir"], "desktopDir": None, "sessionIds": set(), "claudeCli": True}
        with mock.patch.object(vault.subprocess, "run", return_value=result), mock.patch.object(vault, "proc_allowlist", return_value=metadata):
            self.assertTrue(vault.inspect_activity(self.source, self.policy)["busy"])
            metadata["claudeCli"] = False
            self.assertFalse(vault.inspect_activity(self.source, self.policy)["busy"])

    def test_inventory_uses_explicit_titles_and_does_not_write(self):
        inventory = self.vault.inventory(self.source)
        self.assertEqual(len(inventory["sessions"]), 1)
        session = inventory["sessions"][0]
        self.assertEqual(session["label"], "Original research session")
        self.assertNotIn("TEST_PROMPT_SECRET", json.dumps(inventory))
        self.assertEqual(session["parseStatus"], "valid")
        self.assertFalse(self.policy.backups.exists())

    def test_activity_covers_claude_exe_and_keeps_known_other_profile_separate(self):
        result = mock.Mock(returncode=0, stdout=f"{self.policy.uid} 1234 /opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe\n".encode())
        own = {"configDir":self.source["configDir"],"desktopDir":None,"sessionIds":set()}
        other = {**own,"configDir":self.target["configDir"]}
        with mock.patch.object(vault.subprocess,"run",return_value=result):
            with mock.patch.object(vault,"proc_allowlist",return_value=own):
                self.assertTrue(vault.inspect_activity(self.source,self.policy)["busy"])
            with mock.patch.object(vault,"proc_allowlist",return_value=None):
                state = vault.inspect_activity(self.source,self.policy)
                self.assertTrue(state["busy"]); self.assertFalse(state["known"])
            with mock.patch.object(vault,"proc_allowlist",return_value=other):
                state = vault.inspect_activity(self.source,self.policy)
                self.assertFalse(state["busy"]); self.assertTrue(state["known"])

    def test_live_append_marked_unstable_and_all_observed_bytes_retained(self):
        destination = self.policy.backups / "append-test" / "session.jsonl"
        appended = False
        def append_once(path, byte_count):
            nonlocal appended
            if not appended:
                with path.open("ab") as stream:
                    stream.write(b'{"type":"assistant"}\n')
                appended = True
        result = vault.stable_file(self.session, self.policy, destination, after_chunk=append_once)
        self.assertFalse(result["stable"])
        self.assertEqual(destination.read_bytes(), self.session.read_bytes())
        self.assertEqual(result["bytes"], destination.stat().st_size)

    def test_incomplete_tail_is_backed_up_verbatim_and_blocks_resume(self):
        with self.session.open("ab") as stream:
            stream.write(b'{"unfinished":')
        before = self.session.read_bytes()
        result = self.vault.snapshot(self.source)
        self.assertEqual(result["incompleteFiles"], 1)
        copied = Path(result["backupPath"]) / "data/cli" / self.relative
        self.assertEqual(copied.read_bytes(), before)
        self.assertEqual(self.session.read_bytes(), before)
        with self.assertRaises(vault.VaultError):
            self.vault.safe_resume_plan(self.source, self.session_id)

    def test_invalid_full_line_stays_distinct_from_incomplete_tail(self):
        with self.session.open("ab") as stream:
            stream.write(b'NOT_JSON\n')
        inventory = self.vault.inventory(self.source)
        self.assertEqual(inventory["summary"]["invalidFiles"], 1)
        self.assertEqual(inventory["summary"]["incompleteFiles"], 0)
        with self.assertRaises(vault.VaultError):
            self.vault.handoff(self.source, self.target, [self.session_id])

    def test_snapshot_permissions_and_no_auth_device_cookie_copy(self):
        result = self.vault.snapshot(self.source)
        root = Path(result["backupPath"])
        for path in root.rglob("*"):
            self.assertFalse(path.is_symlink())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)
        names = {path.name for path in root.rglob("*")}
        self.assertFalse(names & {".credentials.json", ".claude.json", "config.json", "Cookies"})
        self.assertTrue(any(root.glob("data/desktop/claude-code-sessions/*/*/local_*.json")))

    def test_active_or_unknown_profile_blocks_handoff_but_allows_snapshot(self):
        for activity in ({"known": True, "busy": True, "sessionIds": {self.session_id}},
                         {"known": False, "busy": False, "sessionIds": set()}):
            protected = vault.SessionVault(self.policy, lambda p, policy: activity)
            with self.assertRaises(vault.VaultError) as caught:
                protected.handoff(self.source, self.target, [self.session_id])
            self.assertEqual(caught.exception.code, "active")
            self.assertTrue(protected.snapshot(self.source)["ok"])
        self.assertFalse((Path(self.target["configDir"]) / self.relative).exists())

    def test_symlink_is_not_followed_and_handoff_is_rejected(self):
        link = self.session.parent / self.session_id / "tool-results/leaked.txt"
        link.symlink_to(Path(self.source["configDir"]) / ".credentials.json")
        snapshot = self.vault.snapshot(self.source)
        self.assertEqual(snapshot["symlinksSkipped"], 1)
        self.assertFalse((Path(snapshot["backupPath"]) / "data/cli" / link.relative_to(Path(self.source["configDir"]))).exists())
        with self.assertRaises(vault.VaultError):
            self.vault.handoff(self.source, self.target, [self.session_id])

    def test_paths_and_uuid_escape_rejected(self):
        bad = {**self.target, "configDir": str(self.home / "outside")}
        with self.assertRaises(vault.VaultError):
            self.vault.inventory(bad)
        with self.assertRaises(vault.VaultError):
            self.vault.handoff(self.source, self.target, ["../../.credentials.json"])
        with self.assertRaises(vault.VaultError):
            self.vault.rollback("../../elsewhere")

    def test_conflicts_do_not_overwrite_or_partially_copy(self):
        existing = Path(self.target["configDir"]) / self.relative
        self.write(existing, b"PREEXISTING_TARGET")
        with self.assertRaises(vault.VaultError) as caught:
            self.vault.handoff(self.source, self.target, [self.session_id])
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(existing.read_bytes(), b"PREEXISTING_TARGET")
        self.assertFalse((Path(self.target["configDir"]) / "file-history").exists())

    def test_handoff_preserves_artifacts_and_only_rollback_its_own_files(self):
        result = self.vault.handoff(self.source, self.target, [self.session_id])
        self.assertTrue(result["ok"])
        target = Path(self.target["configDir"])
        self.assertEqual((target / self.relative).read_bytes(), self.session.read_bytes())
        self.assertTrue((target / "projects" / self.project / self.session_id / "subagents/agent-test.jsonl").exists())
        self.assertTrue((target / "file-history" / self.session_id / "v1").exists())
        self.assertFalse((target / ".credentials.json").exists())
        self.assertEqual(list(Path(self.target["desktopDir"]).iterdir()), [])
        self.assertEqual(result["resumeArguments"][0]["args"], ["--resume", self.session_id])
        rollback = self.vault.rollback(result["receiptId"])
        self.assertEqual(rollback["removedFiles"], result["files"])
        self.assertFalse((target / self.relative).exists())
        self.assertTrue(self.session.exists())
        self.assertTrue(Path(result["receiptPath"]).exists())

    def test_rollback_refuses_changed_or_live_target(self):
        result = self.vault.handoff(self.source, self.target, [self.session_id])
        target = Path(self.target["configDir"]) / self.relative
        target.write_bytes(target.read_bytes() + b'{}\n')
        with self.assertRaises(vault.VaultError):
            self.vault.rollback(result["receiptId"])
        self.assertTrue(target.exists())
        busy = vault.SessionVault(self.policy, lambda p, policy: {"known": True, "busy": True, "sessionIds": set()})
        with self.assertRaises(vault.VaultError):
            busy.rollback(result["receiptId"])

    def test_round_trip_newer_transcript_can_return_to_current_and_restore_previous_bytes(self):
        original = self.session.read_bytes()
        self.vault.handoff(self.source, self.target, [self.session_id])
        managed = Path(self.target["configDir"]) / self.relative
        with managed.open("ab") as stream:
            stream.write(b'{"type":"assistant","message":"continued in isolated account"}\n')
        result = self.vault.handoff(self.target, self.source, [self.session_id])
        self.assertTrue(result["ok"])
        self.assertEqual(result["replacedFiles"], 1)
        self.assertGreater(result["reusedFiles"], 0)
        self.assertEqual(self.session.read_bytes(), managed.read_bytes())
        self.assertEqual((Path(self.source["configDir"]) / ".credentials.json").read_bytes(), b"TEST_AUTH_NEVER_COPIED")
        rollback = self.vault.rollback(result["receiptId"])
        self.assertEqual(rollback["restoredFiles"], 1)
        self.assertEqual(self.session.read_bytes(), original)
        self.assertNotEqual(managed.read_bytes(), original)

    def test_target_newer_transcript_is_retained_and_reused(self):
        self.vault.handoff(self.source, self.target, [self.session_id])
        with self.session.open("ab") as stream:
            stream.write(b'{"type":"assistant","message":"newer current context"}\n')
        latest = self.session.read_bytes()
        result = self.vault.handoff(self.target, self.source, [self.session_id])
        self.assertTrue(result["ok"])
        self.assertEqual(result["replacedFiles"], 0)
        self.assertEqual(result["createdFiles"], 0)
        self.assertEqual(result["reusedFiles"], result["files"])
        self.assertEqual(self.session.read_bytes(), latest)
        rollback = self.vault.rollback(result["receiptId"])
        self.assertEqual(rollback["removedFiles"], 0)
        self.assertEqual(rollback["restoredFiles"], 0)
        self.assertEqual(self.session.read_bytes(), latest)

    def test_divergent_context_is_never_merged_or_overwritten(self):
        self.vault.handoff(self.source, self.target, [self.session_id])
        managed = Path(self.target["configDir"]) / self.relative
        for path, branch in ((self.session, "left"), (managed, "right")):
            with path.open("ab") as stream:
                stream.write(json.dumps({"message": branch}).encode() + b"\n")
        before = self.session.read_bytes()
        with self.assertRaises(vault.VaultError) as caught:
            self.vault.handoff(self.target, self.source, [self.session_id])
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(self.session.read_bytes(), before)

    def test_binary_attachment_prefix_is_not_treated_as_appendable_context(self):
        self.vault.handoff(self.source, self.target, [self.session_id])
        managed = Path(self.target["configDir"]) / "file-history" / self.session_id / "v1"
        managed.write_bytes(managed.read_bytes() + b"different binary checkpoint")
        with self.assertRaises(vault.VaultError) as caught:
            self.vault.handoff(self.target, self.source, [self.session_id])
        self.assertEqual(caught.exception.code, "conflict")

    def test_safe_resume_plan_returns_only_explicit_official_arguments(self):
        plan = self.vault.safe_resume_plan(self.source, self.session_id)
        self.assertEqual(plan["args"], ["--resume", self.session_id])
        self.assertEqual(plan["cwd"], str(self.cwd))
        self.assertNotIn("TEST_PROMPT_SECRET", json.dumps(plan))
        self.assertFalse(self.policy.backups.exists())

    def test_publication_race_returns_partial_receipt_without_overwrite(self):
        original = os.link
        count = 0
        def race(source, target, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                Path(target).write_bytes(b"CONCURRENT_USER_FILE")
            return original(source, target, **kwargs)
        with mock.patch.object(vault.os, "link", side_effect=race):
            result = self.vault.handoff(self.source, self.target, [self.session_id])
        self.assertFalse(result["ok"])
        self.assertEqual(result["files"], 1)
        self.assertEqual(result["resumeArguments"], [])
        self.assertTrue(Path(result["receiptPath"]).exists())
        self.vault.rollback(result["receiptId"])
        remaining = list(Path(self.target["configDir"]).rglob("*.jsonl"))
        self.assertTrue(any(p.read_bytes() == b"CONCURRENT_USER_FILE" for p in remaining))

    def test_crash_after_publication_is_recovered_from_staging_identity(self):
        receipts = []
        original = os.link
        def crash(source, target, **kwargs):
            original(source, target, **kwargs)
            raise KeyboardInterrupt("simulated process interruption")
        with mock.patch.object(vault.os, "link", side_effect=crash), self.assertRaises(KeyboardInterrupt):
            self.vault.handoff(self.source, self.target, [self.session_id], receipt_callback=lambda r: receipts.append(r["receiptId"]))
        self.assertTrue(receipts)
        self.assertTrue(self.vault.rollback(receipts[0])["ok"])
        self.assertTrue(self.session.exists())
        self.assertFalse((Path(self.target["configDir"]) / self.relative).exists())

    def test_crash_after_rollback_delete_is_idempotently_recovered(self):
        result = self.vault.handoff(self.source, self.target, [self.session_id])
        receipt, _ = self.vault._load_receipt(result["receiptId"])
        destination = Path(self.target["configDir"]) / next(r["relativePath"] for r in receipt["files"] if r["operation"] == "created")
        original = Path.unlink
        def crash(path, *args, **kwargs):
            original(path, *args, **kwargs)
            if path == destination: raise KeyboardInterrupt("simulated rollback interruption")
        with mock.patch.object(Path, "unlink", crash), self.assertRaises(KeyboardInterrupt):
            self.vault.rollback(result["receiptId"])
        self.assertTrue(self.vault.rollback(result["receiptId"])["ok"])
        self.assertTrue(self.vault.rollback(result["receiptId"])["ok"])

    def test_crash_after_rollback_restore_is_recovered_without_replacing_new_work(self):
        target = Path(self.target["configDir"]) / self.relative
        previous = self.session.read_bytes()
        self.write(target, previous)
        self.write(self.session, previous + b'{"type":"assistant"}\n')
        result = self.vault.handoff(self.source, self.target, [self.session_id])
        original = os.replace
        def crash(source, destination, **kwargs):
            original(source, destination, **kwargs)
            if Path(destination) == target and Path(source).name.startswith(".session-restore-"):
                raise KeyboardInterrupt("simulated restore interruption")
        with mock.patch.object(vault.os, "replace", side_effect=crash), self.assertRaises(KeyboardInterrupt):
            self.vault.rollback(result["receiptId"])
        self.assertTrue(self.vault.rollback(result["receiptId"])["ok"])
        self.assertEqual(target.read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()
