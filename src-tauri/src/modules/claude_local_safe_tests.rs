use super::*;
use std::os::unix::fs::{symlink, PermissionsExt};

struct Fixture {
    home: PathBuf,
    store: Store,
}
impl Fixture {
    fn new() -> Self {
        let home =
            std::env::temp_dir().join(format!("cockpit-claude-safe-test-{}", Uuid::new_v4()));
        fs::create_dir(&home).unwrap();
        fs::set_permissions(&home, fs::Permissions::from_mode(0o700)).unwrap();
        let store = Store {
            root: home.join(".cockpit_accounts/claude_native"),
            home: home.clone(),
        };
        Self { home, store }
    }
    fn private_file(&self, path: &Path, bytes: &[u8]) {
        private_dir(&self.home, &self.store.root, path.parent().unwrap()).unwrap();
        fs::write(path, bytes).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
    }
    fn fake_runner(&self) -> Runner {
        let script = self.home.join("fake-runner.py");
        self.private_file(&script, br#"import json, os, pathlib, sys
assert not any(k.startswith('ANTHROPIC_') or k.startswith('CLAUDE_CODE_OAUTH') or k in ('SSLKEYLOGFILE','NODE_OPTIONS','PYTHONPATH') for k in os.environ)
assert 'TZ' not in os.environ
args = sys.argv[1:]
registry = pathlib.Path(args[args.index('--registry') + 1])
profile_id = args[args.index('--profile-id') + 1] if '--profile-id' in args else None
assert registry.name == 'registry.json'
assert registry.parent.name == 'claude_native'
if args[0] == 'refresh':
    with (registry.parent / 'called.txt').open('a') as f: f.write(profile_id + '\n')
print(json.dumps({'ok': True, 'message': 'TEST_SECRET_MUST_NOT_BE_RELAYED'}))
"#);
        self.private_file(&self.home.join("claude-local-companion.py"), br#"import json, pathlib, sys
assert sys.argv[1] == 'overview'
path = pathlib.Path(sys.argv[sys.argv.index('--registry') + 1])
registry = json.loads(path.read_text())
profiles = []
for profile in registry['profiles']:
    snapshot = path.parent / 'snapshots' / (profile['id'] + '.json')
    saved = json.loads(snapshot.read_text()) if snapshot.exists() else {
        'auth': {'status':'unknown','email':None,'plan':None,'checkedAt':None},
        'usage': {'fiveHour':None,'sevenDay':None,'extraUsage':None,'observedAt':None,'source':'none','error':None}}
    profiles.append(dict(profile, selected=profile['id']==registry['selectedProfileId'], **saved))
print(json.dumps({'profiles':profiles,'selectedProfileId':registry['selectedProfileId'],
    'desktopRunning':False,'codeProcessCount':0,'checkedAt':1,'warning':None}))
"#);
        let python = [
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/usr/bin/python3",
        ]
        .into_iter()
        .map(PathBuf::from)
        .find(|p| p.is_file())
        .unwrap();
        Runner { python, script }
    }
    fn write_snapshot(&self, id: &str, signed_in: bool) {
        let snapshot = Snapshot {
            auth: ClaudeAuth {
                status: if signed_in {
                    AuthStatus::SignedIn
                } else {
                    AuthStatus::SignedOut
                },
                email: Some("fixture@example.invalid".into()),
                plan: Some("pro".into()),
                checked_at: Some(now()),
                source: Some(AuthSource::OfficialCli),
            },
            usage: ClaudeUsage {
                five_hour: Some(WindowUsage {
                    used_percent: 21.0,
                    resets_at: Some(now() + 1000),
                }),
                source: UsageSource::OfficialStatusline,
                observed_at: Some(now()),
                ..ClaudeUsage::default()
            },
        };
        atomic_json(
            &self.home,
            &self.store.root,
            &self
                .store
                .root
                .join("snapshots")
                .join(format!("{}.json", id)),
            &snapshot,
        )
        .unwrap();
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.home);
    }
}

#[test]
fn claude_local_safe_empty_list_does_not_create_registry_or_directories() {
    let f = Fixture::new();
    let registry = f.store.read_registry().unwrap();
    assert!(registry.profiles.is_empty());
    assert!(!f.store.root.exists());
}

#[test]
fn claude_local_safe_archive_metadata_is_compatible_and_blocks_active_commands() {
    let f = Fixture::new();
    f.store.create("selected".into()).unwrap();
    f.store.create("archive".into()).unwrap();
    let mut registry = f.store.read_registry().unwrap();
    let archived = registry.profiles[0].id.clone();
    registry.profiles[0].archived = true;
    registry.profiles[0].archived_at = Some(1);
    registry.profiles[0].auto_refresh = false;
    f.store.write_registry(&registry).unwrap();
    assert!(f.store.require_known_profile(&archived).is_ok());
    assert!(f.store.require_profile(&archived).is_err());
    assert!(f.store.select(&archived).is_err());
    assert!(f.store.preferences(&archived, true, None).is_err());
    assert!(f.store.mark_collector_installed(&archived).is_err());
    assert_eq!(f.store.read_registry().unwrap().selected_profile_id, registry.selected_profile_id);
    registry.selected_profile_id = Some(archived);
    assert!(f.store.write_registry(&registry).is_err());
    let old = serde_json::json!({"id":registry.profiles[1].id,"label":"old","kind":"managed",
        "configDir":registry.profiles[1].config_dir,"desktopDir":registry.profiles[1].desktop_dir,
        "createdAt":1,"collectorInstalled":false,"autoRefresh":true,"renewalDate":null});
    let parsed: ProfileMetadata = serde_json::from_value(old).unwrap();
    assert!(!parsed.archived);
    assert!(parsed.archived_at.is_none());
}

#[test]
fn claude_local_safe_register_current_only_references_known_profile() {
    let f = Fixture::new();
    let (cli, desktop) = f.store.current_paths();
    f.private_file(
        &cli.join(".credentials.json"),
        b"DO_NOT_READ_OR_COPY_TEST_FIXTURE",
    );
    f.private_file(&desktop.join("session-marker"), b"ACTIVE_TEST_SESSION");
    f.store.register_current().unwrap();
    f.store.register_current().unwrap();
    let registry = f.store.read_registry().unwrap();
    assert_eq!(registry.profiles.len(), 1);
    assert_eq!(registry.profiles[0].id, CURRENT_PROFILE_ID);
    assert_eq!(registry.profiles[0].config_dir, cli.to_string_lossy());
    assert!(f.store.root.join("profiles").is_dir());
    assert_eq!(cli, f.home.join(".claude"));
    let saved = fs::read_to_string(f.store.registry_path()).unwrap();
    assert!(!saved.contains("DO_NOT_READ"));
    assert!(!saved.contains("credentials"));
    assert_eq!(
        fs::read(cli.join(".credentials.json")).unwrap(),
        b"DO_NOT_READ_OR_COPY_TEST_FIXTURE"
    );
    assert_eq!(
        fs::read(desktop.join("session-marker")).unwrap(),
        b"ACTIVE_TEST_SESSION"
    );
}

#[test]
fn claude_local_safe_missing_standard_profile_is_not_created_or_imported() {
    let f = Fixture::new();
    assert!(f.store.register_current().is_err());
    assert!(!f.home.join(".claude").exists());
    assert!(!f.store.registry_path().exists());
}

#[test]
fn claude_local_safe_create_private_empty_profile_and_select_without_login() {
    let f = Fixture::new();
    f.store.create("论文研究".into()).unwrap();
    let registry = f.store.read_registry().unwrap();
    let p = &registry.profiles[0];
    validate_id(&p.id).unwrap();
    assert_eq!(p.kind, ProfileKind::Managed);
    assert_eq!(registry.selected_profile_id.as_deref(), Some(p.id.as_str()));
    assert_eq!(fs::read_dir(&p.config_dir).unwrap().count(), 0);
    assert_eq!(fs::read_dir(&p.desktop_dir).unwrap().count(), 0);
    for path in [
        &f.store.root,
        &PathBuf::from(&p.config_dir),
        &PathBuf::from(&p.desktop_dir),
    ] {
        assert_eq!(
            fs::metadata(path).unwrap().permissions().mode() & 0o777,
            0o700
        );
    }
    assert_eq!(
        fs::metadata(f.store.registry_path())
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
    assert!(f.store.select("../../.claude").is_err());
    assert!(f
        .store
        .preferences(&p.id, true, Some("2026-02-30".into()))
        .is_err());
    f.store
        .preferences(&p.id, false, Some("2026-11-12".into()))
        .unwrap();
    let changed = f.store.read_registry().unwrap();
    assert!(!changed.profiles[0].auto_refresh);
    assert_eq!(
        changed.profiles[0].renewal_date.as_deref(),
        Some("2026-11-12")
    );
}

#[test]
fn claude_local_safe_rejects_foreign_paths_and_symlink_profiles() {
    let f = Fixture::new();
    f.store.create("test".into()).unwrap();
    let mut registry = f.store.read_registry().unwrap();
    let original = registry.profiles[0].config_dir.clone();
    registry.profiles[0].config_dir = f.home.join("unrelated").to_string_lossy().into();
    assert!(f.store.validate_registry(&registry).is_err());
    registry.profiles[0].config_dir = original.clone();
    fs::remove_dir(&original).unwrap();
    let unrelated = f.home.join("unrelated");
    fs::create_dir(&unrelated).unwrap();
    fs::set_permissions(&unrelated, fs::Permissions::from_mode(0o700)).unwrap();
    symlink(&unrelated, &original).unwrap();
    assert!(f.store.read_registry().is_err());
}

#[test]
fn claude_local_safe_rejects_symlink_registry_and_public_metadata_file() {
    let f = Fixture::new();
    f.store.initialize().unwrap();
    let registry = f.store.registry_path();
    fs::set_permissions(&registry, fs::Permissions::from_mode(0o644)).unwrap();
    assert!(f.store.read_registry().is_err());
    fs::remove_file(&registry).unwrap();
    f.private_file(&f.home.join("elsewhere.json"), b"{}");
    symlink(f.home.join("elsewhere.json"), &registry).unwrap();
    assert!(f.store.read_registry().is_err());
}

#[test]
fn claude_local_safe_snapshot_drops_raw_errors_and_preserves_unknowns() {
    let snapshot = Snapshot {
        auth: ClaudeAuth {
            status: AuthStatus::SignedIn,
            email: Some("fixture@example.invalid".into()),
            plan: Some("not-a-valid-plan-secret".into()),
            checked_at: Some(1),
            source: Some(AuthSource::OfficialCli),
        },
        usage: ClaudeUsage {
            five_hour: Some(WindowUsage {
                used_percent: 10001.0,
                resets_at: None,
            }),
            error: Some("TEST_SECRET_FROM_CLI".into()),
            ..ClaudeUsage::default()
        },
    };
    let (auth, usage) = sanitize_snapshot(snapshot);
    assert!(auth.plan.is_none());
    assert!(usage.five_hour.is_none());
    assert!(usage.seven_day.is_none());
    assert!(usage.extra_usage.is_none());
    assert!(!usage.error.unwrap().contains("TEST_SECRET"));
}

#[test]
fn claude_local_safe_snapshot_accepts_contract_numbers_without_inventing_currency() {
    let raw = serde_json::json!({
        "auth": {"status":"signed_in", "email":"fixture@example.invalid", "plan":"pro", "checkedAt":123},
        "usage": {"fiveHour":{"usedPercent":101.5,"resetsAt":999}, "sevenDay":null,
            "extraUsage":{"enabled":true,"usedMinor":12.5,"limitMinor":1000,"currency":null},
            "observedAt":123,"source":"official_cli","error":null}
    });
    let (_, usage) = sanitize_snapshot(serde_json::from_value(raw).unwrap());
    assert_eq!(usage.five_hour.unwrap().used_percent, 101.5);
    let extra = usage.extra_usage.unwrap();
    assert_eq!(extra.used_minor, Some(12.5));
    assert_eq!(extra.limit_minor, Some(1000.0));
    assert!(extra.currency.is_none());
}

#[test]
fn claude_local_safe_fake_helper_has_clean_environment_and_fixed_result() {
    let f = Fixture::new();
    f.store.create("test".into()).unwrap();
    let registry = f.store.read_registry().unwrap();
    let id = &registry.profiles[0].id;
    let result = f
        .fake_runner()
        .action(&f.store, id, Action::OpenUsage)
        .unwrap();
    assert!(result.ok);
    assert!(!result.message.contains("TEST_SECRET"));
    assert!(Action::from_user("logout").is_err());
    assert!(Action::from_user("collect").is_err());
}

#[test]
fn claude_local_safe_action_errors_use_only_known_codes() {
    let conflict = Action::InstallCollector.safe_result(false, Some("collector_conflict"));
    assert!(conflict.message.contains("自定义状态栏"));
    assert_eq!(conflict.code.as_deref(), Some("collector_conflict"));
    let unknown = Action::LoginCli.safe_result(false, Some("TEST_SECRET_ERROR_PAYLOAD"));
    assert!(unknown.code.is_none());
    assert!(!unknown.message.contains("TEST_SECRET"));
}

#[test]
fn claude_local_safe_automatic_refresh_all_skips_missing_login_and_disabled_profiles() {
    let f = Fixture::new();
    f.store.create("signed-in".into()).unwrap();
    f.store.create("signed-out".into()).unwrap();
    f.store.create("disabled".into()).unwrap();
    f.store.create("unknown".into()).unwrap();
    let registry = f.store.read_registry().unwrap();
    let ids: Vec<String> = registry.profiles.iter().map(|p| p.id.clone()).collect();
    f.write_snapshot(&ids[0], true);
    f.write_snapshot(&ids[1], false);
    f.write_snapshot(&ids[2], true);
    f.store.preferences(&ids[2], false, None).unwrap();
    f.fake_runner().refresh_all(&f.store, true).unwrap();
    assert_eq!(
        fs::read_to_string(f.store.root.join("called.txt")).unwrap(),
        format!("{}\n", ids[0])
    );
}

#[test]
fn claude_local_safe_automatic_refresh_uses_passive_identity_and_stops_auth_retries() {
    let f = Fixture::new();
    f.store.create("healthy".into()).unwrap();
    f.store.create("expired".into()).unwrap();
    f.store.create("passive-mismatch".into()).unwrap();
    let registry = f.store.read_registry().unwrap();
    let ids: Vec<String> = registry.profiles.iter().map(|p| p.id.clone()).collect();
    for id in &ids { f.write_snapshot(id, true); }
    let path = f.store.root.join("snapshots").join(format!("{}.json", ids[1]));
    let mut snapshot: Snapshot = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    snapshot.usage.error = Some("untrusted text".into());
    snapshot.usage.error_code = Some("auth_expired".into());
    f.private_file(&path, &serde_json::to_vec(&snapshot).unwrap());
    let runner = f.fake_runner();
    let companion_path = f.home.join("claude-local-companion.py");
    let script = fs::read_to_string(&companion_path).unwrap().replace(
        "profiles.append(dict(profile,", "if profile['label'] == 'passive-mismatch': saved['auth']['status'] = 'unknown'\n    profiles.append(dict(profile,");
    f.private_file(&companion_path, script.as_bytes());
    runner.refresh_all(&f.store, true).unwrap();
    assert_eq!(fs::read_to_string(f.store.root.join("called.txt")).unwrap(), format!("{}\n", ids[0]));
}

#[test]
fn claude_local_safe_manual_refresh_all_checks_signed_out_unknown_and_paused_profiles() {
    let f = Fixture::new();
    f.store.create("signed-out".into()).unwrap();
    f.store.create("unknown".into()).unwrap();
    f.store.create("paused".into()).unwrap();
    let registry = f.store.read_registry().unwrap();
    let ids: Vec<String> = registry.profiles.iter().map(|p| p.id.clone()).collect();
    f.write_snapshot(&ids[0], false);
    f.write_snapshot(&ids[2], true);
    f.store.preferences(&ids[2], false, None).unwrap();

    f.fake_runner().refresh_all(&f.store, false).unwrap();

    let called = fs::read_to_string(f.store.root.join("called.txt")).unwrap();
    assert_eq!(
        called.lines().collect::<Vec<_>>(),
        ids.iter().map(String::as_str).collect::<Vec<_>>()
    );
    assert!(!f.store.read_registry().unwrap().profiles[2].auto_refresh);
}

#[test]
fn claude_local_safe_bounded_runner_suppresses_failed_and_oversized_output() {
    let f = Fixture::new();
    let runner = f.fake_runner();
    let mut failure = Command::new(&runner.python);
    failure.args(["-I", "-c", "import sys; print('TEST_SECRET'); sys.exit(1)"]);
    let error = bounded_output(failure, Duration::from_secs(3)).unwrap_err();
    assert_eq!(error, HELPER_ERROR);
    assert!(!error.contains("TEST_SECRET"));
    let mut oversized = Command::new(&runner.python);
    oversized.args(["-I", "-c", "print('x' * 300000)"]);
    assert!(bounded_output(oversized, Duration::from_secs(3)).is_err());
    let mut timeout = Command::new(&runner.python);
    timeout.args(["-I", "-c", "import time; time.sleep(10)"]);
    let started = Instant::now();
    assert!(bounded_output(timeout, Duration::from_millis(80)).is_err());
    assert!(started.elapsed() < Duration::from_secs(2));
}
