#!/usr/bin/env python3
"""Companion security checks using temporary metadata and stubbed native APIs."""
from contextlib import contextmanager, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock
import uuid

sys.dont_write_bytecode = True
SOURCE = Path(__file__).with_name("claude-local-companion.py")
spec = importlib.util.spec_from_file_location("companion_isolation_test", SOURCE)
companion = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = companion
spec.loader.exec_module(companion)
safe = companion.safe


class CompanionFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="claude-companion-test-")
        # TemporaryDirectory may be under macOS /var (a symlink to /private/var).
        self.home = Path(self.temporary.name).resolve()
        self.home.chmod(0o700)
        self.policy = safe.Policy(self.home, os.getuid(), "fixture")
        self.source = self.profile("11111111-1111-4111-8111-111111111111", "Source")
        self.target = self.profile("22222222-2222-4222-8222-222222222222", "Target")
        self.session = "33333333-3333-4333-8333-333333333333"
        self.cwd = self.home / "project with spaces"
        self.mkdir(self.cwd)
        self.rows = [self.source, self.target]
        self.write_registry()
        self.registry = safe.Registry(str(self.policy.registry), self.policy)
        self.runner = safe.Runner(self.registry)
        self.vault = mock.Mock()
        self.vault.safe_resume_plan.return_value = {"ok": True, "args": ["--resume", self.session], "cwd": str(self.cwd)}
        self.vault.inventory.return_value = {"activityKnown": True, "active": False, "sessions": []}
        self.vault.snapshot.return_value = {"ok": True, "unstableFiles": 0, "invalidFiles": 0,
            "incompleteFiles": 0, "unverifiedFiles": 0, "symlinksSkipped": 0}
        self.vault.handoff.return_value = {"ok": True}

    def tearDown(self):
        self.temporary.cleanup()

    def mkdir(self, path):
        cursor = self.home
        for piece in path.relative_to(self.home).parts:
            cursor /= piece
            cursor.mkdir(mode=0o700, exist_ok=True)
            cursor.chmod(0o700)

    def profile(self, ident, label):
        row = {"id": ident, "label": label, "kind": "managed",
               "configDir": str(self.policy.root / "profiles" / ident / "cli"),
               "desktopDir": str(self.policy.root / "profiles" / ident / "desktop"),
               "createdAt": 1700000000, "collectorInstalled": False, "autoRefresh": False, "renewalDate": None}
        self.mkdir(Path(row["configDir"])); self.mkdir(Path(row["desktopDir"]))
        return row

    def write_registry(self):
        self.policy.registry.write_text(json.dumps({"version": 1, "selectedProfileId": self.source["id"], "profiles": self.rows}))
        self.policy.registry.chmod(0o600)

    def test_resume_rejects_active_or_unknown_source_before_auth_and_copy(self):
        with mock.patch.object(companion, "session_vault", return_value=self.vault), \
             mock.patch.object(self.runner, "busy", return_value=True), \
             mock.patch.object(self.runner, "auth_status") as auth, \
             mock.patch.object(self.runner, "require_preflight") as preflight:
            with self.assertRaises(safe.SafeError) as caught:
                companion.resume(self.runner, self.source, self.target, self.session)
        self.assertEqual(caught.exception.code, "busy")
        auth.assert_not_called(); preflight.assert_not_called()
        self.vault.handoff.assert_not_called(); self.vault.snapshot.assert_not_called()

    def test_resume_rejects_unknown_target_activity(self):
        self.vault.inventory.return_value = {"activityKnown": False, "active": False}
        with mock.patch.object(companion, "session_vault", return_value=self.vault), \
             mock.patch.object(self.runner, "busy", return_value=False), \
             mock.patch.object(self.runner, "auth_status") as auth:
            with self.assertRaises(safe.SafeError) as caught:
                companion.resume(self.runner, self.source, self.target, self.session)
        self.assertEqual(caught.exception.code, "busy")
        auth.assert_not_called(); self.vault.handoff.assert_not_called()

    def test_resumable_requires_the_vault_verified_plan(self):
        self.vault.safe_resume_plan.side_effect = safe.SafeError("invalid")
        with self.assertRaises(safe.SafeError):
            companion.resumable(self.vault, self.source, self.session)
        self.vault.safe_resume_plan.assert_called_once_with(self.source, self.session)
        self.vault.inventory.assert_not_called()

    def test_resumable_rejects_foreign_arguments_and_symlink_cwd(self):
        self.vault.safe_resume_plan.return_value = {"ok": True, "args": ["--print", "not-a-resume"], "cwd": str(self.cwd)}
        with self.assertRaises(safe.SafeError):
            companion.resumable(self.vault, self.source, self.session)
        symlink = self.home / "redirected-project"
        symlink.symlink_to(self.cwd, target_is_directory=True)
        self.vault.safe_resume_plan.return_value = {"ok": True, "args": ["--resume", self.session], "cwd": str(symlink)}
        with self.assertRaises(safe.SafeError):
            companion.resumable(self.vault, self.source, self.session)

    def test_same_profile_resume_rejects_every_incomplete_backup_category(self):
        for key in ("unstableFiles", "invalidFiles", "incompleteFiles", "unverifiedFiles", "symlinksSkipped"):
            self.vault.snapshot.return_value = {"ok": True, key: 1}
            with mock.patch.object(companion, "session_vault", return_value=self.vault), \
                 mock.patch.object(self.runner, "busy", return_value=False), \
                 mock.patch.object(self.runner, "require_preflight"), \
                 mock.patch.object(self.runner, "auth_status", return_value={"status": "signed_in"}), \
                 mock.patch.object(self.runner, "run_bounded") as launch:
                with self.assertRaises(safe.SafeError):
                    companion.resume(self.runner, self.source, self.source, self.session)
            launch.assert_not_called()

    def test_target_must_be_authenticated_before_handoff(self):
        with mock.patch.object(companion, "session_vault", return_value=self.vault), \
             mock.patch.object(self.runner, "busy", return_value=False), \
             mock.patch.object(self.runner, "require_preflight"), \
             mock.patch.object(self.runner, "auth_status", return_value={"status": "signed_out"}):
            with self.assertRaises(safe.SafeError) as caught:
                companion.resume(self.runner, self.source, self.target, self.session)
        self.assertEqual(caught.exception.code, "signed_out")
        self.vault.handoff.assert_not_called()

    def test_cross_profile_resume_preserves_selected_env_and_uses_vault_handoff(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "SECRET_POISON", "CLAUDE_CODE_OAUTH_TOKEN": "SECRET_POISON"}), \
             mock.patch.object(companion, "session_vault", return_value=self.vault), \
             mock.patch.object(self.runner, "busy", return_value=False), \
             mock.patch.object(self.runner, "require_preflight"), \
             mock.patch.object(self.runner, "auth_status", return_value={"status": "signed_in"}) as auth, \
             mock.patch.object(self.runner, "run_bounded", return_value=(0, b"")) as launch:
            result = companion.resume(self.runner, self.source, self.target, self.session)
        self.assertTrue(result["ok"])
        self.vault.handoff.assert_called_once_with(self.source, self.target, [self.session])
        # The vault's handoff operation owns its archive; do not duplicate it.
        self.vault.snapshot.assert_not_called()
        env = auth.call_args.args[1]
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], self.target["configDir"])
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        self.assertNotIn("TZ", env)
        command = launch.call_args.args[0]
        self.assertEqual(command[:3], ["/usr/bin/open", "-a", "Terminal"])
        launcher = Path(command[3])
        self.assertEqual(stat.S_IMODE(launcher.stat().st_mode), 0o700)
        self.assertIn("resume-child", launcher.read_text())
        self.assertNotIn("SECRET_POISON", launcher.read_text())

    def test_terminal_child_rechecks_activity_after_preflight(self):
        with mock.patch.object(companion, "session_vault", return_value=self.vault), \
             mock.patch.object(self.runner, "busy", side_effect=[False, False, True]), \
             mock.patch.object(self.runner, "require_preflight"), \
             mock.patch.object(self.runner, "auth_status") as auth, \
             mock.patch.object(companion.os, "chdir") as chdir, \
             mock.patch.object(companion.os, "execve") as execute:
            with self.assertRaises(safe.SafeError) as caught:
                companion.resume(self.runner, self.source, self.target, self.session, terminal_child=True)
        self.assertEqual(caught.exception.code, "busy")
        self.assertGreaterEqual(self.vault.safe_resume_plan.call_count, 2)
        auth.assert_not_called(); chdir.assert_not_called(); execute.assert_not_called()
        self.vault.handoff.assert_not_called(); self.vault.snapshot.assert_not_called()

    def test_terminal_child_executes_only_official_resume_without_status_or_copy(self):
        class ExecReplaced(BaseException):
            pass
        with mock.patch.object(companion, "session_vault", return_value=self.vault), \
             mock.patch.object(self.runner, "busy", return_value=False), \
             mock.patch.object(self.runner, "require_preflight"), \
             mock.patch.object(self.runner, "auth_status") as auth, \
             mock.patch.object(companion.os, "chdir") as chdir, \
             mock.patch.object(companion.os, "execve", side_effect=ExecReplaced) as execute:
            with self.assertRaises(ExecReplaced):
                companion.resume(self.runner, self.source, self.target, self.session, terminal_child=True)
        binary, args, env = execute.call_args.args
        self.assertEqual(binary, str(self.policy.cli))
        self.assertEqual(args, [binary, "--resume", self.session])
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], self.target["configDir"])
        chdir.assert_called_once_with(self.cwd)
        auth.assert_not_called(); self.vault.handoff.assert_not_called(); self.vault.snapshot.assert_not_called()

    def test_main_holds_both_locks_in_stable_order_and_redacts_errors(self):
        sequence = []
        @contextmanager
        def lock(profile):
            sequence.append(("enter", profile["id"]))
            try:
                yield
            finally:
                sequence.append(("exit", profile["id"]))
        self.runner.lock = lock
        captured = io.StringIO()
        with mock.patch.object(safe.Policy, "production", return_value=self.policy), \
             mock.patch.object(safe, "Runner", return_value=self.runner), \
             mock.patch.object(companion, "resume", side_effect=RuntimeError("SECRET_RAW_EXCEPTION")), \
             redirect_stdout(captured):
            code = companion.main(["resume", "--registry", str(self.policy.registry), "--profile-id", self.target["id"],
                                   "--target-profile-id", self.source["id"], "--session-id", self.session])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(captured.getvalue())["ok"])
        self.assertNotIn("SECRET_RAW_EXCEPTION", captured.getvalue())
        self.assertEqual(sequence, [("enter", self.source["id"]), ("enter", self.target["id"]),
                                    ("exit", self.target["id"]), ("exit", self.source["id"])])

    def test_overview_projects_cached_identity_without_secrets_or_raw_processes(self):
        account, organization = str(uuid.uuid4()), str(uuid.uuid4())
        self.source["newSecretField"] = "SECRET_REGISTRY"
        self.write_registry()
        records = {
            Path(self.source["configDir"]) / ".claude.json": {"oauthAccount": {
                "emailAddress": "fixture@example.invalid", "accountUuid": account, "organizationUuid": organization,
                "planDisplayName": "Claude Pro", "accessToken": "SECRET_OAUTH", "expiresAt": 9999999999999}},
            Path(self.source["desktopDir"]) / "config.json": {
                "lastKnownAccountUuid": account, "oauth:tokenCache": "SECRET_DESKTOP"},
            Path(self.source["desktopDir"]) / "plan-usage-history.json": {"version": 2, "samples": [
                {"t": 1700000000000, "org": organization, "u": {"fh": 12, "sd": 27}}]},
        }
        for path, value in records.items():
            path.write_text(json.dumps(value)); path.chmod(0o600)
        with mock.patch.object(safe.Runner, "processes", return_value=[{"desktop": True, "code": False, "private": "SECRET_PROCESS"}]), \
             mock.patch.object(safe.Runner, "auth_status") as auth, \
             mock.patch.object(safe.Runner, "query_usage") as usage, \
             mock.patch.object(safe.Runner, "save_snapshot") as save:
            result = companion.overview(self.policy)
        serialized = json.dumps(result)
        self.assertNotIn("SECRET_", serialized)
        self.assertNotIn(account, serialized); self.assertNotIn(organization, serialized)
        row = next(row for row in result["profiles"] if row["id"] == self.source["id"])
        self.assertEqual(row["auth"]["email"], "fixture@example.invalid")
        self.assertEqual(row["auth"]["status"], "unknown")
        self.assertEqual(row["usage"]["source"], "desktop_cache")
        self.assertEqual(row["usage"]["observedAt"], 1700000000)
        self.assertTrue(result["desktopRunning"])
        auth.assert_not_called(); usage.assert_not_called(); save.assert_not_called()

    def test_passive_overview_reads_all_profiles_and_isolates_broken_snapshot(self):
        good = {"auth": {**safe.empty_auth(), "status": "signed_in", "source": "official_cli"},
                "usage": {**safe.empty_usage(), "source": "desktop_cache", "observedAt": 1700000000}}
        seen = []
        def cached(profile):
            seen.append(profile["id"])
            if profile["id"] == self.source["id"]:
                raise TypeError("SECRET_INVALID_SNAPSHOT_FIELD")
            return good
        with mock.patch.object(safe.Runner, "cached_snapshot", side_effect=cached), \
             mock.patch.object(safe.Runner, "processes", return_value=[]), \
             mock.patch.object(safe.Runner, "auth_status") as auth, \
             mock.patch.object(safe.Runner, "query_usage") as query, \
             mock.patch.object(safe.Runner, "save_snapshot") as save:
            result = companion.overview(self.policy)
        self.assertEqual(seen, [self.source["id"], self.target["id"]])
        self.assertEqual(result["profiles"][0]["auth"]["status"], "error")
        self.assertEqual(result["profiles"][1]["usage"]["observedAt"], 1700000000)
        self.assertNotIn("SECRET_INVALID", json.dumps(result))
        auth.assert_not_called(); query.assert_not_called(); save.assert_not_called()

    def test_archived_profiles_are_metadata_only_and_cannot_read_sessions(self):
        self.target.update(archived=True, archivedAt=1700000000, autoRefresh=False)
        self.write_registry()
        seen = []
        def cached(profile):
            self.assertNotEqual(profile["id"], self.target["id"])
            seen.append(profile["id"])
            return {"auth":safe.empty_auth(),"usage":safe.empty_usage()}
        with mock.patch.object(safe.Runner,"cached_snapshot",side_effect=cached), \
             mock.patch.object(safe.Runner,"processes",return_value=[]):
            overview = companion.overview(self.policy)
        self.assertEqual(seen,[self.source["id"]])
        self.assertEqual([p["id"] for p in overview["profiles"]],[self.source["id"]])
        self.assertEqual(set(overview["archivedProfiles"][0]),{"id","label","kind","archivedAt"})
        output = io.StringIO()
        with mock.patch.object(safe.Policy,"production",return_value=self.policy), \
             mock.patch.object(companion,"session_inventory") as inventory, redirect_stdout(output):
            code = companion.main(["sessions","--registry",str(self.policy.registry),"--profile-id",self.target["id"]])
        self.assertEqual(code,1); inventory.assert_not_called()

    def test_sibling_helper_rejects_group_write_and_symlinks_before_import(self):
        path = self.home / "unsafe.py"
        path.write_text("raise AssertionError('must not execute')\n")
        path.chmod(0o666)
        with mock.patch.object(companion, "__file__", str(self.home / "companion.py")):
            with self.assertRaises(RuntimeError):
                companion.load_sibling("unsafe_fixture", path.name)
            path.chmod(0o600)
            link = self.home / "linked.py"; link.symlink_to(path)
            with self.assertRaises(RuntimeError):
                companion.load_sibling("unsafe_fixture", link.name)


if __name__ == "__main__":
    unittest.main()
