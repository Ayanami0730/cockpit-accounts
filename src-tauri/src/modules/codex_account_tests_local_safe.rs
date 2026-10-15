// Codex 账号测试：local-safe 切号的凭据回读与只写 auth.json。
use super::{
    local_safe_adopt_credentials_from_auth_json, local_safe_adopt_credentials_from_codex_home,
    local_safe_auth_json_value, local_safe_live_account_id, local_safe_mark_current,
    local_safe_write_credentials,
};

fn local_safe_tokens(email: &str, account_id: &str, exp: i64, refresh_token: &str) -> CodexTokens {
    let id_token = make_jwt(serde_json::json!({
        "aud": ["codex-cli"],
        "iss": "https://auth.openai.com",
        "email": email,
        "sub": "user-local-safe",
        "exp": 4_102_444_800i64,
        "https://api.openai.com/auth": {
            "chatgpt_user_id": "user-local-safe",
            "chatgpt_plan_type": "pro",
            "chatgpt_subscription_active_until": "2026-10-01T00:00:00Z",
            "account_id": account_id,
        }
    }));
    let access_token = make_jwt(serde_json::json!({
        "sub": "access-local-safe",
        "exp": exp,
        "https://api.openai.com/auth": { "chatgpt_account_id": account_id }
    }));
    CodexTokens {
        id_token,
        access_token,
        refresh_token: Some(refresh_token.to_string()),
    }
}

fn local_safe_saved_account(email: &str, account_id: &str, exp: i64, refresh_token: &str) -> CodexAccount {
    upsert_account(local_safe_tokens(email, account_id, exp, refresh_token)).expect("save account")
}

fn local_safe_auth_json(email: &str, account_id: &str, exp: i64, refresh_token: &str) -> String {
    let mut account = CodexAccount::new(
        "fixture".to_string(),
        email.to_string(),
        local_safe_tokens(email, account_id, exp, refresh_token),
    );
    account.account_id = Some(account_id.to_string());
    serde_json::to_string(&build_auth_file_value(&account).expect("auth value")).expect("json")
}

const LOCAL_SAFE_EXP: i64 = 4_000_000_000;

#[test]
fn local_safe_adopts_fresher_live_credentials_and_keeps_newer_saved_ones() {
    let _lock = crate::modules::test_support::env_lock()
        .lock()
        .unwrap_or_else(|err| err.into_inner());
    let env = TestEnvGuard::new("codex-local-safe-adopt");
    let saved = local_safe_saved_account("a@example.invalid", "acc-a", LOCAL_SAFE_EXP, "rt-saved");

    fs::write(
        env.codex_home().join("auth.json"),
        local_safe_auth_json("a@example.invalid", "acc-a", LOCAL_SAFE_EXP + 100, "rt-rotated"),
    )
    .unwrap();
    let adoption = local_safe_adopt_credentials_from_codex_home(&env.codex_home())
        .unwrap()
        .expect("matched");
    assert_eq!(adoption.account_id, saved.id);
    assert!(adoption.updated);
    assert_eq!(
        load_account(&saved.id).unwrap().tokens.refresh_token.as_deref(),
        Some("rt-rotated")
    );

    fs::write(
        env.codex_home().join("auth.json"),
        local_safe_auth_json("a@example.invalid", "acc-a", LOCAL_SAFE_EXP - 100, "rt-stale"),
    )
    .unwrap();
    let adoption = local_safe_adopt_credentials_from_codex_home(&env.codex_home())
        .unwrap()
        .expect("matched");
    assert!(!adoption.updated);
    assert_eq!(
        load_account(&saved.id).unwrap().tokens.refresh_token.as_deref(),
        Some("rt-rotated")
    );
}

#[test]
fn local_safe_ignores_credentials_of_accounts_it_does_not_manage() {
    let _lock = crate::modules::test_support::env_lock()
        .lock()
        .unwrap_or_else(|err| err.into_inner());
    let env = TestEnvGuard::new("codex-local-safe-unknown");
    let saved = local_safe_saved_account("a@example.invalid", "acc-a", LOCAL_SAFE_EXP, "rt-a");

    let other = local_safe_auth_json("b@example.invalid", "acc-b", LOCAL_SAFE_EXP + 100, "rt-b");
    fs::write(env.codex_home().join("auth.json"), &other).unwrap();
    assert!(local_safe_adopt_credentials_from_codex_home(&env.codex_home()).unwrap().is_none());
    assert!(local_safe_adopt_credentials_from_auth_json(&other).unwrap().is_none());
    assert!(local_safe_adopt_credentials_from_auth_json("not json").unwrap().is_none());
    assert_eq!(load_account(&saved.id).unwrap().tokens.refresh_token.as_deref(), Some("rt-a"));
}

#[test]
fn local_safe_adopts_remote_auth_json_body() {
    let _lock = crate::modules::test_support::env_lock()
        .lock()
        .unwrap_or_else(|err| err.into_inner());
    let _env = TestEnvGuard::new("codex-local-safe-remote");
    let saved = local_safe_saved_account("a@example.invalid", "acc-a", LOCAL_SAFE_EXP, "rt-a");

    let remote = local_safe_auth_json("a@example.invalid", "acc-a", LOCAL_SAFE_EXP + 50, "rt-remote");
    let adoption = local_safe_adopt_credentials_from_auth_json(&remote).unwrap().expect("matched");
    assert!(adoption.updated);
    assert_eq!(
        load_account(&saved.id).unwrap().tokens.refresh_token.as_deref(),
        Some("rt-remote")
    );
}

#[test]
fn local_safe_switch_writes_only_auth_json_and_marks_current() {
    let _lock = crate::modules::test_support::env_lock()
        .lock()
        .unwrap_or_else(|err| err.into_inner());
    let env = TestEnvGuard::new("codex-local-safe-write");
    let target = local_safe_saved_account("b@example.invalid", "acc-b", LOCAL_SAFE_EXP, "rt-b");

    let config = "model = \"gpt-6-astra\"\nservice_tier = \"priority\"\nmodel_reasoning_effort = \"ultra\"\n";
    fs::write(env.codex_home().join("config.toml"), config).unwrap();
    fs::write(
        env.codex_home().join("auth.json"),
        local_safe_auth_json("a@example.invalid", "acc-a", LOCAL_SAFE_EXP, "rt-a"),
    )
    .unwrap();

    local_safe_write_credentials(&env.codex_home(), &target).expect("write credentials");
    local_safe_mark_current(&target.id).expect("mark current");

    assert_eq!(fs::read_to_string(env.codex_home().join("config.toml")).unwrap(), config);
    let written: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(env.codex_home().join("auth.json")).unwrap()).unwrap();
    assert_eq!(written["tokens"]["refresh_token"], "rt-b");
    assert_eq!(written["tokens"]["account_id"], "acc-b");
    assert_eq!(written["auth_mode"], "chatgpt");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = fs::metadata(env.codex_home().join("auth.json")).unwrap().permissions().mode();
        assert_eq!(mode & 0o777, 0o600);
    }
    assert_eq!(load_account_index().current_account_id.as_deref(), Some(target.id.as_str()));
    assert_eq!(local_safe_live_account_id().as_deref(), Some(target.id.as_str()));
}

#[test]
fn local_safe_refuses_to_project_incomplete_credentials() {
    let account = CodexAccount::new(
        "fixture".to_string(),
        "a@example.invalid".to_string(),
        CodexTokens { id_token: String::new(), access_token: "at".to_string(), refresh_token: None },
    );
    assert!(local_safe_auth_json_value(&account).is_err());
}
