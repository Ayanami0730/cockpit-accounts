#!/usr/bin/env python3
"""Isolated tests: temporary profiles and a fake CLI; never a real account."""
import importlib.util
from contextlib import redirect_stdout as contextlib_redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

sys.dont_write_bytecode = True
SOURCE = Path(__file__).with_name("claude-local-safe-runner.py")
spec = importlib.util.spec_from_file_location("claude_safe_runner", SOURCE)
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claude-safe-test-")
        self.home = Path(self.temp.name)
        self.home.chmod(0o700)
        self.policy = runner.Policy(self.home, os.getuid(), "fixture-user")
        self.ident = str(uuid.uuid4())
        self.profile = {"id": self.ident, "label": "Test account", "kind": "managed",
                        "configDir": str(self.policy.root / "profiles" / self.ident / "cli"),
                        "desktopDir": str(self.policy.root / "profiles" / self.ident / "desktop"),
                        "createdAt": 1700000000, "collectorInstalled": False, "autoRefresh": True,
                        "renewalDate": None}
        self.rows = [self.profile]
        self.private_dir(Path(self.profile["configDir"]))
        self.private_dir(Path(self.profile["desktopDir"]))
        self.write_registry()
        self.app = runner.Runner(runner.Registry(str(self.policy.registry), self.policy))

    def tearDown(self):
        self.temp.cleanup()

    def private_dir(self, path):
        relative = path.relative_to(self.home)
        cursor = self.home
        for piece in relative.parts:
            cursor /= piece
            cursor.mkdir(exist_ok=True, mode=0o700)
            cursor.chmod(0o700)

    def write_registry(self):
        self.private_dir(self.policy.root)
        self.policy.registry.write_text(json.dumps({"version": 1, "selectedProfileId": self.ident,
                                                    "profiles": self.rows}))
        self.policy.registry.chmod(0o600)

    def fake_cli(self, mode="normal"):
        self.private_dir(self.policy.cli.parent)
        self.policy.cli.write_text("#!" + sys.executable + "\n" + """
import json, sys, time
from pathlib import Path
MODE = MODE_LITERAL
if sys.argv[1:] == ['--version']:
    print('3.2.1 (Claude Code)'); sys.exit(0)
if sys.argv[1:] == ['auth', 'status']:
    if MODE == 'signed_out':
        print(json.dumps({'loggedIn': False})); sys.exit(1)
    print(json.dumps({'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'firstParty',
                      'email': 'fixture@example.invalid', 'subscriptionType': 'pro',
                      'unknown_secret': 'must-never-reach-the-snapshot'}))
    sys.exit(0)
for line in sys.stdin:
    request = json.loads(line)
    with (Path(__file__).parents[7] / 'wire.jsonl').open('a') as f:
        f.write(json.dumps(request) + '\\n')
    if request['type'] != 'control_request':
        raise RuntimeError('A model/user message was sent')
    subtype = request['request']['subtype']
    if subtype == 'get_usage':
        if request['request'].get('skip_behaviors') is not True:
            raise RuntimeError('Transcript scan was enabled')
        if MODE == 'timeout':
            time.sleep(20)
        if MODE == 'flood':
            print('x' * 300000, flush=True); continue
        if MODE == 'malformed':
            print('not-json-secret-value', flush=True); continue
        if MODE == 'tool_request':
            print(json.dumps({'type':'control_request','request_id':'tool','request':{'subtype':'can_use_tool'}}),flush=True); continue
        if MODE == 'unsupported':
            print(json.dumps({'type':'control_response','response':{'subtype':'error','request_id':request['request_id'],'error':'raw secret-server-detail'}}),flush=True); continue
        if MODE in ('disabled_error', 'expired_error', 'network_error', 'forbidden_error'):
            error = {'disabled_error': 'Your account has been disabled. SECRET',
                     'expired_error': {'type':'token_expired','message':'SECRET'},
                     'network_error': {'code':'ETIMEDOUT','message':'SECRET'},
                     'forbidden_error': {'status':403,'message':'Forbidden SECRET'}}[MODE]
            print(json.dumps({'type':'control_response','response':{'subtype':'error','request_id':request['request_id'],'error':error}}),flush=True); continue
        data = {'subscription_type':'pro','rate_limits_available':True,'rate_limits':{
            'five_hour':{'utilization':23.5,'resets_at':'2026-10-12T12:00:00Z'},
            'seven_day':{'utilization':67,'resets_at':None},
            'extra_usage':{'is_enabled':True,'used_credits':1250,'monthly_limit':5000,'currency':'USD'}},
            'untrusted_extra':'raw secret payload'}
    else:
        data = {'commands': [], 'models': []}
    print(json.dumps({'type':'system','subtype':'keep_alive','ignored':'raw secret payload'}),flush=True)
    print(json.dumps({'type':'control_response','response':{'subtype':'success','request_id':'unrelated','response':{}}}),flush=True)
    print(json.dumps({'type':'control_response','response':{'subtype':'success','request_id':request['request_id'],'response':data}}),flush=True)
""".replace("MODE_LITERAL", repr(mode)))
        self.policy.cli.chmod(0o700)
        # Resolve the test transcript path inside this temporary home only.
        source = self.policy.cli.read_text().replace("Path(__file__).parents[7] / 'wire.jsonl'", repr(str(self.home / "wire.jsonl")))
        source = source.replace("with (" + repr(str(self.home / "wire.jsonl")) + ").open('a')", "with Path(" + repr(str(self.home / "wire.jsonl")) + ").open('a')")
        self.policy.cli.write_text(source)

    def saved(self):
        return json.loads(self.app.snapshot_path(self.profile).read_text())


class PathTests(Fixture):
    def test_private_native_root_bootstraps_below_valid_existing_parent(self):
        isolated = self.home / "bootstrap-home"
        isolated.mkdir(mode=0o700)
        policy = runner.Policy(isolated, os.getuid(), "fixture-user")
        policy.root.parent.mkdir(mode=0o700)
        runner.ensure_private_dir(policy.root / "snapshots", policy)
        self.assertEqual(stat.S_IMODE(policy.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((policy.root / "snapshots").stat().st_mode), 0o700)

    def test_private_native_root_never_creates_unvalidated_shared_parent(self):
        isolated = self.home / "bootstrap-missing-parent"
        isolated.mkdir(mode=0o700)
        policy = runner.Policy(isolated, os.getuid(), "fixture-user")
        with self.assertRaises(runner.SafeError):
            runner.ensure_private_dir(policy.root, policy)
        self.assertFalse(policy.root.parent.exists())

    def test_registry_must_be_exact_fixed_location(self):
        with self.assertRaises(runner.SafeError):
            runner.Registry(str(self.policy.registry.parent / "other.json"), self.policy)

    def test_rejects_profile_escape_and_current_identity_mismatch(self):
        for change in ({"configDir": str(self.home)}, {"kind": "current"},
                       {"id": "../../escape"}, {"desktopDir": "/tmp/not-allowed"}):
            original = self.profile.copy()
            self.profile.update(change)
            self.write_registry()
            with self.assertRaises(runner.SafeError):
                runner.Registry(str(self.policy.registry), self.policy)
            self.profile.clear(); self.profile.update(original)

    def test_rejects_symlink_profile_and_registry(self):
        cli = Path(self.profile["configDir"])
        cli.rmdir()
        target = self.home / "elsewhere"; target.mkdir(mode=0o700)
        cli.symlink_to(target, target_is_directory=True)
        with self.assertRaises(runner.SafeError):
            runner.Registry(str(self.policy.registry), self.policy)
        cli.unlink(); cli.mkdir(mode=0o700)
        moved = self.policy.root / "saved.json"
        self.policy.registry.rename(moved)
        self.policy.registry.symlink_to(moved)
        with self.assertRaises(runner.SafeError):
            runner.Registry(str(self.policy.registry), self.policy)

    def test_rejects_world_readable_private_registry(self):
        self.policy.registry.chmod(0o644)
        with self.assertRaises(runner.SafeError):
            runner.Registry(str(self.policy.registry), self.policy)

    def test_current_registration_only_accepts_exact_known_paths(self):
        self.private_dir(self.policy.current_cli); self.private_dir(self.policy.current_desktop)
        self.profile.update(id=runner.CURRENT_ID, kind="current", configDir=str(self.policy.current_cli),
                            desktopDir=str(self.policy.current_desktop))
        self.ident = runner.CURRENT_ID
        self.write_registry()
        registry = runner.Registry(str(self.policy.registry), self.policy)
        self.assertEqual(registry.get(self.ident)["kind"], "current")


class EnvironmentTests(Fixture):
    def native_process_rows(self, config):
        executable = str(self.home / ".local/share/claude/versions/3.2.1")
        rows = f"{os.getuid()} 1234 {executable}\n".encode()
        env = executable + " HOME=" + str(self.home)
        if config is not None:
            env += " CLAUDE_CONFIG_DIR=" + config
        return [(0, rows), (0, env.encode())]

    def test_native_version_executable_marks_managed_profile_busy(self):
        with mock.patch.object(self.app, "run_bounded", side_effect=self.native_process_rows(self.profile["configDir"])):
            self.assertTrue(self.app.busy(self.profile))

    def test_native_version_executable_conservatively_marks_current_busy(self):
        current = {**self.profile, "id": runner.CURRENT_ID, "kind": "current"}
        with mock.patch.object(self.app, "run_bounded", side_effect=self.native_process_rows(None)):
            self.assertTrue(self.app.busy(current))

    def test_native_version_executable_blocks_cli_login_before_preflight(self):
        with mock.patch.object(self.app, "run_bounded", side_effect=self.native_process_rows(self.profile["configDir"])), mock.patch.object(self.app, "require_preflight") as preflight:
            with self.assertRaises(runner.SafeError) as caught:
                self.app.terminal(self.profile, "login_cli")
        self.assertEqual(caught.exception.code, "login_busy")
        preflight.assert_not_called()

    def test_busy_detector_includes_standalone_official_cli(self):
        executable = str(self.home / ".vscode/extensions/anthropic.claude-code-3.2.1-darwin-arm64/resources/native-binary/claude")
        rows = f"{os.getuid()} 1234 {executable}\n".encode()
        environment = (executable + " HOME=" + str(self.home) + " CLAUDE_CONFIG_DIR=" + self.profile["configDir"] + " SOME_OTHER=value").encode()
        with mock.patch.object(self.app, "run_bounded", side_effect=[(0, rows), (0, environment)]):
            processes = self.app.processes()
        self.assertEqual(len(processes), 1)
        self.assertTrue(processes[0]["code"])
        self.assertEqual(processes[0]["config"], self.profile["configDir"])

    def test_npm_node_process_is_classified_and_blocks_its_profile(self):
        process = f"{self.policy.uid} 4321 /usr/local/bin/node\n".encode()
        command = f"node /opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/cli.js CLAUDE_CONFIG_DIR={self.profile['configDir']} HOME={self.home}".encode()
        with mock.patch.object(self.app, "run_bounded", side_effect=[(0, process), (0, command)]):
            rows = self.app.processes()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["code"])
        self.assertEqual(rows[0]["config"], self.profile["configDir"])
        with mock.patch.object(self.app, "processes", return_value=rows):
            self.assertTrue(self.app.busy(self.profile))

    def test_unknown_process_config_is_conservatively_busy(self):
        with mock.patch.object(self.app, "processes", return_value=[
            {"pid": 1234, "desktop": False, "code": True, "executable": "/some/claude", "config": None}
        ]):
            self.assertTrue(self.app.busy(self.profile))

    def test_environment_is_whitelist_and_managed_shell_stays_in_selected_profile(self):
        poison = {"ANTHROPIC_AUTH_TOKEN": "do-not-copy", "CLAUDE_CONFIG_DIR": "/other-account",
                  "CLAUDE_SECURESTORAGE_CONFIG_DIR": "/other-credentials", "CLAUDE_CODE_OAUTH_TOKEN": "do-not-copy",
                  "HOMEBREW_API_DOMAIN": "https://mirror.invalid", "NODE_OPTIONS": "--require evil.js"}
        with mock.patch.dict(os.environ, poison, clear=True):
            env = self.app.profile_env(self.profile)
        for key in poison:
            if key != "CLAUDE_CONFIG_DIR":
                self.assertNotIn(key, env)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], self.profile["configDir"])
        self.assertTrue(Path(env["SHELL"]).is_relative_to(self.policy.root))
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            self.assertNotIn(key, env)
        script = Path(env["SHELL"])
        self.assertEqual(stat.S_IMODE(script.stat().st_mode), 0o700)
        if Path("/bin/zsh").exists():
            check = subprocess.run([str(script), "-c", '/usr/bin/env'], env={**env, **poison}, capture_output=True, text=True)
            values = dict(line.split("=", 1) for line in check.stdout.splitlines() if "=" in line)
            self.assertEqual(check.returncode, 0)
            self.assertEqual(values["CLAUDE_CONFIG_DIR"], self.profile["configDir"])
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", values)
            self.assertNotIn("HOMEBREW_API_DOMAIN", values)
            self.assertNotIn("NODE_OPTIONS", values)


class UsageTests(Fixture):
    def test_units_nulls_and_minor_currency_are_preserved(self):
        data = {"rate_limits_available": True, "rate_limits": {
            "five_hour": {"utilization": 41.2, "resets_at": "2026-10-12T12:00:00+00:00"},
            "seven_day": None,
            "extra_usage": {"is_enabled": False, "used_credits": 1250, "monthly_limit": None, "currency": "USD"}}}
        usage = runner.normalize_usage(data)
        self.assertEqual(usage["fiveHour"], {"usedPercent": 41.2, "resetsAt": 1791806400})
        self.assertIsNone(usage["sevenDay"])
        self.assertEqual(usage["extraUsage"]["usedMinor"], 1250)
        self.assertIsNone(usage["extraUsage"]["limitMinor"])

    def test_unavailable_and_malformed_usage_are_not_zero(self):
        candidates = [{"rate_limits_available": False, "rate_limits": {}},
                      {"rate_limits_available": True, "rate_limits": None},
                      {"rate_limits_available": True, "rate_limits": {"five_hour": {"utilization": float("nan")}}},
                      {"rate_limits_available": True, "rate_limits": {"five_hour": {"utilization": True}}},
                      {"rate_limits_available": True, "rate_limits": {"five_hour": {"utilization": 20, "resets_at": "not-a-time"}}}]
        for data in candidates:
            with self.assertRaises(runner.SafeError):
                runner.normalize_usage(data)

    def test_busy_current_skips_cli_and_network_retains_snapshot(self):
        prior = {"auth": runner.empty_auth(), "usage": runner.empty_usage()}
        prior["usage"].update(fiveHour={"usedPercent": 44, "resetsAt": None}, observedAt=1700000000, source="official_cli")
        self.app.save_snapshot(self.profile, prior)
        with mock.patch.object(self.app, "busy", return_value=True), mock.patch.object(self.app, "require_preflight") as preflight, mock.patch.object(self.app, "auth_status") as auth:
            result = self.app.refresh(self.profile)
        self.assertFalse(result["ok"]); preflight.assert_not_called(); auth.assert_not_called()
        saved = self.saved()
        self.assertEqual(saved["usage"]["observedAt"], 1700000000)
        self.assertEqual(saved["usage"]["fiveHour"]["usedPercent"], 44)
        self.assertEqual(saved["usage"]["error"], runner.SAFE_MESSAGES["busy"])
        self.assertEqual(saved["usage"]["errorCode"], "busy")

    def test_account_change_then_usage_failure_never_relabels_old_usage(self):
        self.fake_cli()
        prior = {"auth": {**runner.empty_auth(), "status": "signed_in", "email": "previous@example.invalid",
                          "source": "official_cli", "checkedAt": 1700000000}, "usage": runner.empty_usage()}
        prior["usage"].update(fiveHour={"usedPercent": 88, "resetsAt": None}, observedAt=1700000000, source="official_cli")
        self.app.save_snapshot(self.profile, prior)
        with mock.patch.object(self.app, "busy", return_value=False), mock.patch.object(self.app, "require_preflight"), \
             mock.patch.object(self.app, "query_usage", side_effect=runner.SafeError("timeout")):
            self.app.refresh(self.profile)
        saved = self.saved()
        self.assertEqual(saved["auth"]["email"], "fixture@example.invalid")
        self.assertIsNone(saved["usage"]["fiveHour"])
        self.assertIsNone(saved["usage"]["observedAt"])
        self.assertEqual(saved["usage"]["errorCode"], "network_unavailable")

    def test_official_error_evidence_is_conservative_and_does_not_echo_payloads(self):
        examples = [
            ({"status": 403, "message": "Forbidden"}, "usage"),
            ({"type": "permission_error", "message": "Access denied"}, "usage"),
            ("Your account has been disabled. SECRET", "account_disabled"),
            ("Your account is not disabled", "usage"),
            ({"type": "authentication_error", "message": "SECRET"}, "auth"),
            ("OAuth token has expired. SECRET", "auth_expired"),
            ({"code": "ETIMEDOUT", "message": "SECRET"}, "network_failure"),
            ("Network error: SECRET", "network_failure"),
            ("A help page says your account is suspended", "usage"),
        ]
        for payload, expected in examples:
            actual = runner.official_error_code(payload)
            self.assertEqual(actual, expected)
            self.assertNotIn("SECRET", json.dumps(runner.action_error(actual)))

    def test_auth_status_recognizes_explicit_expiration_without_persisting_raw_output(self):
        with mock.patch.object(self.app, "run_bounded", return_value=(1, json.dumps(
                {"error": {"type": "token_expired", "message": "SECRET"}}).encode())):
            with self.assertRaises(runner.SafeError) as caught:
                self.app.auth_status(self.profile, {})
        self.assertEqual(caught.exception.code, "auth_expired")

    def test_official_control_errors_survive_as_safe_distinct_snapshot_codes(self):
        for mode, expected in [("disabled_error", "account_disabled"), ("expired_error", "auth_expired"),
                               ("network_error", "network_unavailable"), ("forbidden_error", "usage_unavailable")]:
            self.app.save_health(self.profile, runner.empty_health())
            self.fake_cli(mode)
            with mock.patch.object(self.app, "busy", return_value=False), mock.patch.object(self.app, "require_preflight"):
                result = self.app.refresh(self.profile)
            self.assertFalse(result["ok"])
            self.assertEqual(self.saved()["usage"]["errorCode"], expected)
            self.assertNotIn("SECRET", self.app.snapshot_path(self.profile).read_text())

    def test_complete_refresh_uses_only_two_control_requests(self):
        self.fake_cli()
        with mock.patch.object(self.app, "busy", return_value=False), mock.patch.object(self.app, "require_preflight"):
            result = self.app.refresh(self.profile)
        self.assertTrue(result["ok"], result)
        frames = [json.loads(line) for line in (self.home / "wire.jsonl").read_text().splitlines()]
        self.assertEqual([f["request"]["subtype"] for f in frames], ["initialize", "get_usage"])
        self.assertTrue(frames[1]["request"]["skip_behaviors"])
        text = self.app.snapshot_path(self.profile).read_text()
        self.assertNotIn("raw secret", text); self.assertNotIn("must-never", text)
        self.assertEqual(self.saved()["auth"]["status"], "signed_in")
        self.assertEqual(stat.S_IMODE(self.app.snapshot_path(self.profile).stat().st_mode), 0o600)

    def test_signed_out_never_opens_usage_protocol(self):
        self.fake_cli("signed_out")
        with mock.patch.object(self.app, "busy", return_value=False), mock.patch.object(self.app, "require_preflight"):
            result = self.app.refresh(self.profile)
        self.assertFalse(result["ok"])
        self.assertEqual(self.saved()["auth"]["status"], "signed_out")
        self.assertFalse((self.home / "wire.jsonl").exists())

    def test_protocol_rejects_error_tool_request_malformed_and_unbounded_lines(self):
        for mode in ("unsupported", "tool_request", "malformed", "flood"):
            self.fake_cli(mode)
            with mock.patch.object(self.app, "busy", return_value=False), mock.patch.object(self.app, "require_preflight"):
                result = self.app.refresh(self.profile)
            self.assertFalse(result["ok"], mode)
            self.assertNotIn("raw", json.dumps(result))
            self.assertNotIn("secret", self.app.snapshot_path(self.profile).read_text())

    def test_protocol_timeout_terminates_only_own_child(self):
        self.fake_cli("timeout")
        env = self.app.profile_env(self.profile)
        process = subprocess.Popen([str(self.policy.cli)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=env)
        channel = runner.ProcessChannel(process, timeout=3)
        try:
            channel.request("initialize")
            channel.deadline = time.monotonic() + 0.1
            with self.assertRaises(runner.SafeError) as caught:
                channel.request("get_usage", skip_behaviors=True)
            self.assertEqual(caught.exception.code, "timeout")
        finally:
            channel.close()
        self.assertIsNotNone(process.poll())


class PassiveIdentityTests(Fixture):
    def test_manual_official_suspension_is_separate_durable_and_prevents_requests(self):
        before = {"auth": {**runner.empty_auth(), "status":"signed_in", "source":"official_cli", "checkedAt":123},
                  "usage": {**runner.empty_usage(), "observedAt":123, "source":"official_cli"}}
        self.app.save_snapshot(self.profile, before)
        self.app.record_health(self.profile, "account_disabled", "supported_countries_policy", "official_email", runner.now())
        with mock.patch.object(self.app, "require_preflight") as network, mock.patch.object(self.app, "auth_status") as auth:
            result = self.app.refresh(self.profile)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "account_disabled")
        network.assert_not_called(); auth.assert_not_called()
        self.assertEqual(self.app.read_snapshot(self.profile), before)
        self.assertEqual(self.app.read_health(self.profile)["status"], "account_disabled")
        with self.assertRaises(runner.SafeError):
            self.app.record_health(self.profile, "available", "usage_page_available", "official_ui", runner.now())
        self.app.record_health(self.profile, "unknown", "officially_restored", "official_email", runner.now())
        self.assertEqual(self.app.read_health(self.profile)["status"], "unknown")

    def test_manual_usage_page_evidence_is_dated_and_not_a_model_test(self):
        observed = runner.now() - 1200
        self.app.record_health(self.profile, "available", "usage_page_available", "official_ui", observed)
        health = self.app.read_health(self.profile)
        self.assertEqual(health["observedAt"], observed)
        self.assertEqual(health["lastSuccessfulAt"], observed)
        self.assertEqual(health["reason"], "usage_page_available")
        self.assertIsNone(self.app.read_snapshot(self.profile)["usage"]["observedAt"])

    def test_reauth_notice_is_not_suspension_and_busy_skip_does_not_refresh_evidence_age(self):
        observed = runner.now() - 1200
        self.app.record_health(self.profile, "auth_required", "reauth_required", "official_ui", observed)
        self.app.observe_health(self.profile, "busy")
        health = self.app.read_health(self.profile)
        self.assertEqual(health["status"], "auth_required")
        self.assertEqual(health["observedAt"], observed)
        self.assertEqual(health["lastAttemptOutcome"], "busy")

    def test_manual_evidence_rejects_unbounded_or_invented_reasons(self):
        for status, reason, source, observed in [("account_disabled", "HTTP403", "official_ui", runner.now()),
                                               ("available", "usage_page_available", "official_email", runner.now()),
                                               ("account_disabled", "official_notice", "raw_secret", runner.now()),
                                               ("account_disabled", "official_notice", "official_email", runner.now()+10000)]:
            with self.assertRaises(runner.SafeError):
                self.app.record_health(self.profile, status, reason, source, observed)

    def write_metadata(self, *, desktop_matches=True, matching_history=True):
        self.account_id = str(uuid.uuid4())
        self.org_id = str(uuid.uuid4())
        self.other_org = str(uuid.uuid4())
        cli_path = Path(self.profile["configDir"]) / ".claude.json"
        desktop_path = Path(self.profile["desktopDir"]) / "config.json"
        history_path = Path(self.profile["desktopDir"]) / "plan-usage-history.json"
        cli_path.write_text(json.dumps({
            "oauthAccount": {"emailAddress": "real-fixture@example.invalid", "accountUuid": self.account_id,
                "organizationUuid": self.org_id, "planDisplayName": "Claude Pro",
                "accessToken": "SENTINEL_NEVER_DECODE_" + "x" * 10000,
                "expiresAt": 987654321012345678901234567890},
            "credentials": {"refreshToken": "SENTINEL_NEVER_DECODE_REFRESH"}}))
        desktop_path.write_text(json.dumps({"lastKnownAccountUuid": self.account_id if desktop_matches else str(uuid.uuid4()),
                                            "oauth:tokenCacheV2": "SENTINEL_NEVER_DECODE_DESKTOP"}))
        history_path.write_text(json.dumps({"version": 2, "samples": [
            {"t": 1700000000000, "org": self.org_id if matching_history else self.other_org, "u": {"fh": 17.5, "sd": 44}},
            {"t": 1700000200000, "org": self.org_id if matching_history else self.other_org, "u": {"fh": 22, "sd": 49}},
            {"t": 1700000300000, "org": self.other_org, "u": {"fh": 99, "sd": 99}},
        ]}))
        for path in (cli_path, desktop_path, history_path):
            path.chmod(0o600)
        return cli_path, desktop_path, history_path

    def test_passive_projection_never_decodes_credentials_or_expiry(self):
        self.write_metadata()
        original_loads = json.loads
        observed = []
        def guarded_loads(value, *args, **kwargs):
            text = value.decode() if isinstance(value, bytes) else value
            self.assertNotIn("SENTINEL_NEVER_DECODE", text)
            self.assertNotIn("987654321012345678901234567890", text)
            observed.append(text)
            return original_loads(value, *args, **kwargs)
        with mock.patch.object(runner.json, "loads", side_effect=guarded_loads):
            snapshot = self.app.cached_snapshot(self.profile)
        self.assertEqual(snapshot["auth"]["email"], "real-fixture@example.invalid")
        self.assertEqual(snapshot["auth"]["plan"], "pro")
        self.assertEqual(snapshot["auth"]["status"], "unknown")
        self.assertEqual(snapshot["auth"]["source"], "cached_metadata")
        self.assertTrue(observed)
        self.assertNotIn("SENTINEL_NEVER_DECODE", json.dumps(snapshot))
        self.assertNotIn(self.account_id, json.dumps(snapshot))
        self.assertNotIn(self.org_id, json.dumps(snapshot))

    def test_busy_inspection_never_runs_cli_network_login_or_termination(self):
        paths = self.write_metadata()
        original = {str(path): path.read_bytes() for path in paths}
        with mock.patch.object(self.app, "busy", return_value=True) as busy, \
             mock.patch.object(self.app, "auth_status") as auth, \
             mock.patch.object(self.app, "query_usage") as usage, \
             mock.patch.object(self.app, "require_preflight") as preflight, \
             mock.patch.object(self.app, "terminal") as login, \
             mock.patch.object(runner.subprocess, "Popen") as popen, \
             mock.patch.object(runner.os, "kill") as kill:
            result = self.app.inspect_auth(self.profile)
        self.assertTrue(result["ok"])
        for forbidden in (busy, auth, usage, preflight, login, popen, kill):
            forbidden.assert_not_called()
        for path in paths:
            self.assertEqual(path.read_bytes(), original[str(path)])
        saved = self.saved()
        self.assertEqual(saved["usage"]["source"], "desktop_cache")
        self.assertEqual(saved["usage"]["observedAt"], 1700000200)
        self.assertEqual(saved["usage"]["fiveHour"], {"usedPercent": 22, "resetsAt": None})
        self.assertEqual(saved["usage"]["sevenDay"], {"usedPercent": 49, "resetsAt": None})
        self.assertIsNone(saved["usage"]["extraUsage"])

    def test_current_profile_is_read_without_any_official_command(self):
        self.private_dir(self.policy.current_cli); self.private_dir(self.policy.current_desktop)
        self.profile.update(id=runner.CURRENT_ID, kind="current", configDir=str(self.policy.current_cli),
                            desktopDir=str(self.policy.current_desktop))
        self.ident = runner.CURRENT_ID
        self.write_registry()
        self.app = runner.Runner(runner.Registry(str(self.policy.registry), self.policy))
        paths = self.write_metadata()
        (Path(self.profile["configDir"]) / ".claude.json").replace(self.home / ".claude.json")
        with mock.patch.object(self.app, "run_bounded") as commands:
            result = self.app.inspect_auth(self.profile)
        self.assertTrue(result["ok"])
        commands.assert_not_called()
        self.assertEqual(self.saved()["auth"]["source"], "cached_metadata")

    def test_cli_metadata_without_desktop_login_never_adopts_desktop_usage(self):
        _, desktop, _ = self.write_metadata()
        desktop.unlink()
        snapshot = self.app.cached_snapshot(self.profile)
        self.assertEqual(snapshot["auth"]["email"], "real-fixture@example.invalid")
        self.assertEqual(snapshot["auth"]["source"], "cached_metadata")
        self.assertIsNone(snapshot["usage"]["observedAt"])

    def test_account_mismatch_cannot_adopt_other_identity_or_history(self):
        self.write_metadata(desktop_matches=False)
        snapshot = self.app.cached_snapshot(self.profile)
        self.assertEqual(snapshot["auth"]["status"], "unknown")
        self.assertIsNone(snapshot["auth"]["email"])
        self.assertIsNone(snapshot["auth"]["source"])
        self.assertIsNone(snapshot["usage"]["fiveHour"])
        self.assertEqual(snapshot["usage"]["errorCode"], "identity_mismatch")

    def verified_snapshot(self):
        return {"auth": {"status": "signed_in", "email": "real-fixture@example.invalid", "plan": "pro",
                         "checkedAt": 1700000001, "source": "official_cli"}, "usage": runner.empty_usage()}

    def test_missing_passive_metadata_preserves_last_official_identity_and_time(self):
        before = self.verified_snapshot()
        self.app.save_snapshot(self.profile, before)
        with mock.patch.object(self.app, "run_bounded") as commands:
            result = self.app.cached_snapshot(self.profile)
        self.assertEqual(result["auth"], before["auth"])
        commands.assert_not_called()

    def test_same_passive_identity_keeps_official_result_while_updating_desktop_usage(self):
        self.write_metadata()
        before = self.verified_snapshot()
        before["usage"].update(error=runner.SAFE_MESSAGES["busy"], errorCode="busy")
        self.app.save_snapshot(self.profile, before)
        with mock.patch.object(self.app, "run_bounded") as commands:
            result = self.app.inspect_auth(self.profile)
        self.assertTrue(result["ok"])
        self.assertEqual(self.saved()["auth"], before["auth"])
        self.assertEqual(self.saved()["usage"]["observedAt"], 1700000200)
        self.assertEqual(self.saved()["usage"]["errorCode"], "busy")
        commands.assert_not_called()

    def test_changed_passive_identity_discards_old_usage_even_if_new_sample_is_older(self):
        self.write_metadata()
        before = self.verified_snapshot()
        before["auth"]["email"] = "previous@example.invalid"
        before["usage"].update(fiveHour={"usedPercent": 99, "resetsAt": None}, observedAt=1800000000, source="official_cli")
        self.app.save_snapshot(self.profile, before)
        result = self.app.cached_snapshot(self.profile)
        self.assertEqual(result["auth"]["status"], "unknown")
        self.assertEqual(result["usage"]["fiveHour"]["usedPercent"], 22)
        self.assertEqual(result["usage"]["observedAt"], 1700000200)

    def test_old_passive_identity_cannot_undo_official_signed_out(self):
        self.write_metadata()
        before = self.verified_snapshot()
        before["auth"].update(status="signed_out", email=None, plan=None)
        self.app.save_snapshot(self.profile, before)
        self.assertEqual(self.app.cached_snapshot(self.profile)["auth"], before["auth"])

    def test_no_matching_history_preserves_existing_usage_byte_values(self):
        self.write_metadata(matching_history=False)
        before = {"auth": runner.empty_auth(), "usage": runner.empty_usage()}
        before["usage"].update(fiveHour={"usedPercent": 11, "resetsAt": 1700000999}, observedAt=1600000000,
                               source="official_cli", error=runner.SAFE_MESSAGES["busy"], errorCode="busy")
        self.app.save_snapshot(self.profile, before)
        self.app.inspect_auth(self.profile)
        self.assertEqual(self.saved()["usage"], before["usage"])

    def test_cached_snapshot_is_pure_and_does_not_replace_newer_live_usage(self):
        self.write_metadata()
        before = {"auth": runner.empty_auth(), "usage": runner.empty_usage()}
        before["usage"].update(fiveHour={"usedPercent": 60, "resetsAt": None}, observedAt=1800000000,
                               source="official_statusline")
        self.app.save_snapshot(self.profile, before)
        saved_bytes = self.app.snapshot_path(self.profile).read_bytes()
        with mock.patch.object(self.app, "save_snapshot") as save, mock.patch.object(self.app, "lock") as lock:
            result = self.app.cached_snapshot(self.profile)
        save.assert_not_called(); lock.assert_not_called()
        self.assertEqual(result["usage"], before["usage"])
        self.assertEqual(self.app.snapshot_path(self.profile).read_bytes(), saved_bytes)

    def test_legacy_snapshot_auth_source_defaults_to_null(self):
        old = {"auth": {"status": "unknown", "email": None, "plan": None, "checkedAt": None},
               "usage": runner.empty_usage()}
        self.app.save_snapshot(self.profile, old)
        self.assertIsNone(self.app.read_snapshot(self.profile)["auth"]["source"])


class CollectorAndActionTests(Fixture):
    def test_native_symlink_discovery_and_newer_version(self):
        self.fake_cli()
        target = self.home / ".local/share/claude/versions/3.2.1"
        self.private_dir(target.parent)
        self.policy.cli.rename(target)
        (self.home / ".local/bin/claude").symlink_to(target)
        with mock.patch.object(self.app, "verify_signature", return_value=True) as signature:
            result = self.app.preflight()
        self.assertTrue(result["ok"])
        self.assertEqual(result["cliVersion"], "3.2.1")
        self.assertEqual(result["cliPath"], str(self.home / ".local/bin/claude"))
        signature.assert_called_once()
        self.assertEqual(set(result), {"ok", "checkedAt", "cliPath", "cliVersion", "checks"})

    def test_npm_symlink_checks_package_identity_and_bin_mapping(self):
        entry = self.home / "npm/lib/node_modules/@anthropic-ai/claude-code/cli.js"
        self.private_dir(entry.parent)
        entry.write_text("#!/usr/bin/env node\n"); entry.chmod(0o700)
        package = entry.parent / "package.json"
        package.write_text(json.dumps({"name": "@anthropic-ai/claude-code", "bin": {"claude": "cli.js"}}))
        package.chmod(0o600)
        self.private_dir(self.policy.cli.parent)
        self.policy.cli.symlink_to(entry)
        with mock.patch.object(self.app, "verify_signature") as signature:
            self.assertEqual(self.app.verify_cli_installation(self.policy.cli), "npm")
        signature.assert_not_called()
        package.write_text(json.dumps({"name": "other-package", "bin": {"claude": "cli.js"}}))
        with self.assertRaises(runner.SafeError):
            self.app.verify_cli_installation(self.policy.cli)

    def test_nvm_npm_installation_works_without_a_login_shell_path(self):
        prefix = self.home / ".nvm/versions/node/v22.20.0"
        entry = prefix / "lib/node_modules/@anthropic-ai/claude-code/cli.js"
        self.private_dir(entry.parent); self.private_dir(prefix / "bin")
        entry.write_text("#!/usr/bin/env node\n"); entry.chmod(0o700)
        package = entry.parent / "package.json"
        package.write_text(json.dumps({"name": "@anthropic-ai/claude-code", "bin": {"claude": "cli.js"}})); package.chmod(0o600)
        (prefix / "bin/claude").symlink_to(entry)
        node = prefix / "bin/node"
        node.write_text("#!" + sys.executable + "\nprint('3.2.1 (Claude Code)')\n"); node.chmod(0o700)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.policy.cli, prefix / "bin/claude")
            result = self.app.preflight()
        self.assertTrue(result["ok"])
        self.assertEqual(result["cliVersion"], "3.2.1")

    def test_admin_group_writable_installation_directory_is_supported(self):
        self.fake_cli()
        self.policy.cli.parent.chmod(0o775)
        with mock.patch.object(runner.grp, "getgrnam", return_value=mock.Mock(gr_gid=os.getgid())), mock.patch.object(self.app, "verify_signature", return_value=True):
            self.assertTrue(self.app.preflight()["ok"])
        # Writable by all users remains unsafe even if it has the admin group.
        self.policy.cli.parent.chmod(0o777)
        with mock.patch.object(runner.grp, "getgrnam", return_value=mock.Mock(gr_gid=os.getgid())):
            self.assertFalse(self.app.preflight()["ok"])

    def test_missing_cli_fails_without_starting_a_process(self):
        with mock.patch.object(self.app, "run_bounded") as launch:
            result = self.app.preflight()
        self.assertFalse(result["ok"])
        self.assertIsNone(result["cliPath"])
        launch.assert_not_called()

    def test_writable_installation_is_rejected_before_launch(self):
        self.fake_cli()
        self.policy.cli.chmod(0o777)
        with mock.patch.object(self.app, "run_bounded") as launch:
            result = self.app.preflight()
        self.assertFalse(result["ok"])
        launch.assert_not_called()

    def test_normal_network_is_default_and_explicit_proxy_is_preserved(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            env = self.app.profile_env(self.profile)
        self.assertFalse(any("proxy" in key.lower() for key in env))
        self.assertNotIn("TZ", env)
        self.assertNotIn("BROWSER", env)
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy.example.invalid:8080", "NO_PROXY": "localhost"}, clear=True):
            env = self.app.profile_env(self.profile)
        self.assertEqual(env["HTTPS_PROXY"], "http://proxy.example.invalid:8080")
        self.assertEqual(env["NO_PROXY"], "localhost")

    def test_standard_profile_keeps_default_cli_config_convention(self):
        profile = {**self.profile, "id": runner.CURRENT_ID, "kind": "current", "configDir": str(self.policy.current_cli)}
        env = self.app.profile_env(profile)
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)
        self.assertEqual(self.policy.current_cli, self.home / ".claude")
        self.assertTrue(self.policy.current_desktop.is_relative_to(self.policy.root))

    def test_login_executes_documented_official_auth_command(self):
        with mock.patch.object(self.app, "require_preflight"), mock.patch.object(self.app, "busy", return_value=False), mock.patch.object(runner.os, "execve") as launch:
            self.app._terminal_child(self.profile, "login_cli")
        self.assertEqual(launch.call_args.args[1], [str(self.policy.cli), "auth", "login"])
        self.assertEqual(launch.call_args.args[2]["CLAUDE_CONFIG_DIR"], self.profile["configDir"])

    def test_usage_opens_default_browser_without_requiring_cli(self):
        with mock.patch.object(self.app, "run_bounded", return_value=(0, b"")) as command:
            self.assertTrue(self.app.open_usage()["ok"])
        self.assertEqual(command.call_args.args[0], ["/usr/bin/open", "https://claude.ai/settings/usage"])

    def test_unknown_arguments_never_echo_input(self):
        output = io.StringIO()
        with contextlib_redirect_stdout(output):
            exit_code = runner.main(["--unexpected-secret-value"])
        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "invalid_profile")
        self.assertNotIn("unexpected-secret-value", output.getvalue())

    def test_snapshot_does_not_accept_untyped_secret_fields(self):
        snapshot = {"auth": runner.empty_auth(), "usage": runner.empty_usage()}
        snapshot["usage"]["fiveHour"] = {"usedPercent": 20, "resetsAt": None, "token": "untrusted"}
        self.app.save_snapshot(self.profile, snapshot)
        with self.assertRaises(runner.SafeError):
            self.app.read_snapshot(self.profile)

    def test_collector_does_not_overwrite_existing_custom_command(self):
        path = Path(self.profile["configDir"]) / "settings.json"
        original = '{"statusLine":{"type":"command","command":"my-own-statusline"},"theme":"dark"}'
        path.write_text(original); path.chmod(0o600)
        with self.assertRaises(runner.SafeError) as caught:
            self.app.install_collector(self.profile)
        self.assertEqual(caught.exception.code, "collector_conflict")
        self.assertEqual(path.read_text(), original)

    def test_collector_installs_idempotently_and_projects_safe_payload(self):
        self.assertTrue(self.app.install_collector(self.profile)["ok"])
        self.assertTrue(self.app.install_collector(self.profile)["ok"])
        data = {"rate_limits": {"five_hour": {"used_percentage": 12.5, "resets_at": 1791806400}},
                "workspace": {"current_dir": "/private-project"}, "credential": "secret-unrelated"}
        result = self.app.collect(self.profile, json.dumps(data).encode())
        self.assertEqual(result, "Claude 5h 12.5% · 7d ?")
        saved = self.app.snapshot_path(self.profile).read_text()
        self.assertNotIn("private-project", saved); self.assertNotIn("secret-unrelated", saved)
        self.assertIsNone(self.saved()["usage"]["sevenDay"])
        self.assertEqual(self.saved()["usage"]["source"], "official_statusline")

    def test_statusline_sample_does_not_clear_explicit_authentication_block(self):
        before = {"auth": runner.empty_auth(), "usage": runner.empty_usage()}
        before["usage"].update(error=runner.SAFE_MESSAGES["auth_expired"], errorCode="auth_expired")
        self.app.save_snapshot(self.profile, before)
        self.app.collect(self.profile, json.dumps({"rate_limits": {"five_hour": {"used_percentage": 12}}}).encode())
        self.assertEqual(self.saved()["usage"]["source"], "official_statusline")
        self.assertEqual(self.saved()["usage"]["errorCode"], "auth_expired")

    def test_desktop_busy_never_runs_preflight_or_launch(self):
        with mock.patch.object(self.app, "busy", return_value=True), mock.patch.object(self.app, "require_preflight") as preflight, mock.patch.object(runner.subprocess, "Popen") as popen:
            with self.assertRaises(runner.SafeError) as caught:
                self.app.launch_desktop(self.profile)
        self.assertEqual(caught.exception.code, "desktop_busy")
        preflight.assert_not_called(); popen.assert_not_called()

    def test_desktop_launch_has_no_forced_network_or_locale_flags(self):
        with mock.patch.object(self.app, "busy", return_value=False), mock.patch.object(self.app, "require_preflight"), mock.patch.object(self.app, "verify_signature", return_value=True), mock.patch.object(runner.subprocess, "Popen") as launch:
            self.assertTrue(self.app.launch_desktop(self.profile)["ok"])
        args = launch.call_args.args[0]
        self.assertEqual(args[1:], ["--user-data-dir=" + self.profile["desktopDir"]])
        self.assertNotIn("TZ", launch.call_args.kwargs["env"])

    def test_snapshot_symlink_is_not_followed(self):
        root = self.policy.root / "snapshots"; root.mkdir(mode=0o700)
        target = self.home / "unrelated"; target.write_text("unchanged"); target.chmod(0o600)
        self.app.snapshot_path(self.profile).symlink_to(target)
        with self.assertRaises(runner.SafeError):
            self.app.save_snapshot(self.profile, {"auth": runner.empty_auth(), "usage": runner.empty_usage()})
        self.assertEqual(target.read_text(), "unchanged")


if __name__ == "__main__":
    unittest.main()
