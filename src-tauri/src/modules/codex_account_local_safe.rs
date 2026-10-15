// Codex 账号模块：local-safe 切号所需的最小凭据操作。
// 只写 auth.json（或用户配置的官方凭据存储），不改 config.toml、会话库或实例配置。

#[derive(Debug, Clone, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LocalSafeAdoption {
    pub account_id: String,
    pub email: String,
    pub updated: bool,
}

fn local_safe_snapshot_is_not_older(
    account: &CodexAccount,
    snapshot: &LocalCodexOAuthSnapshot,
) -> bool {
    let stored =
        codex_oauth::jwt_token_expiration_timestamp(&account.tokens.access_token).unwrap_or(0);
    let incoming =
        codex_oauth::jwt_token_expiration_timestamp(&snapshot.tokens.access_token).unwrap_or(0);
    incoming >= stored
}

fn local_safe_adopt_snapshot(
    snapshot: LocalCodexOAuthSnapshot,
) -> Result<Option<LocalSafeAdoption>, String> {
    for mut account in list_accounts() {
        if account.is_api_key_auth()
            || account.is_agent_identity_auth()
            || account.is_web_session_auth()
        {
            continue;
        }
        if !local_oauth_snapshot_matches_account(&snapshot, &account) {
            continue;
        }
        let updated = if local_safe_snapshot_is_not_older(&account, &snapshot) {
            let changed = apply_local_oauth_snapshot(&mut account, &snapshot);
            if changed {
                save_account(&account)?;
            }
            changed
        } else {
            false
        };
        return Ok(Some(LocalSafeAdoption {
            account_id: account.id.clone(),
            email: account.email.clone(),
            updated,
        }));
    }
    Ok(None)
}

/// Adopts the credentials stored in an official Codex home when they belong to a saved
/// account and are at least as fresh as the saved copy. Codex rotates refresh tokens, so the
/// copy in the live home is the only one guaranteed to still refresh.
pub fn local_safe_adopt_credentials_from_codex_home(
    base_dir: &Path,
) -> Result<Option<LocalSafeAdoption>, String> {
    let Some(snapshot) = load_local_oauth_snapshot_from_official_store(base_dir) else {
        return Ok(None);
    };
    local_safe_adopt_snapshot(snapshot)
}

/// Same as `local_safe_adopt_credentials_from_codex_home`, for an `auth.json` body fetched
/// from another machine. The body is parsed in memory and never touches the keychain.
pub fn local_safe_adopt_credentials_from_auth_json(
    content: &str,
) -> Result<Option<LocalSafeAdoption>, String> {
    let Ok(auth_file) = serde_json::from_str::<CodexAuthFile>(content) else {
        return Ok(None);
    };
    if is_auth_mode_apikey(auth_file.auth_mode.as_deref()) {
        return Ok(None);
    }
    let Some(snapshot) = load_local_oauth_snapshot_from_auth_file(auth_file) else {
        return Ok(None);
    };
    local_safe_adopt_snapshot(snapshot)
}

fn local_safe_require_switchable(account: &CodexAccount) -> Result<CodexAccount, String> {
    if account.is_api_key_auth()
        || account.is_agent_identity_auth()
        || account.is_web_session_auth()
    {
        return Err("本地版只支持切换 ChatGPT 登录的 Codex 账号".to_string());
    }
    if account.tokens.access_token.trim().is_empty() || account.tokens.id_token.trim().is_empty()
    {
        return Err("账号缺少完整的登录凭据，请重新登录后再切换".to_string());
    }
    resolve_account_for_bundle_write(Path::new(""), account)
}

/// The `auth.json` body Codex expects for this account, without merging any local file.
pub fn local_safe_auth_json_value(account: &CodexAccount) -> Result<serde_json::Value, String> {
    let resolved = local_safe_require_switchable(account)?;
    build_auth_file_value(&resolved)
}

/// Writes only the credential store of `base_dir`. `config.toml` stays untouched.
pub fn local_safe_write_credentials(
    base_dir: &Path,
    account: &CodexAccount,
) -> Result<&'static str, String> {
    let resolved = local_safe_require_switchable(account)?;
    fs::create_dir_all(base_dir).map_err(|e| format!("创建 Codex 目录失败: {}", e))?;
    let value = build_merged_auth_file_value(base_dir, &resolved)?;
    write_auth_value_to_configured_store(base_dir, &base_dir.join("auth.json"), &value)
}

pub fn local_safe_mark_current(account_id: &str) -> Result<CodexAccount, String> {
    let mut account =
        load_account(account_id).ok_or_else(|| format!("账号不存在: {}", account_id))?;
    let mut index = load_account_index();
    index.current_account_id = Some(account_id.to_string());
    save_account_index(&index)?;
    account.update_last_used();
    save_account(&account)?;
    Ok(account)
}

/// The saved account whose credentials are currently in the default Codex home.
pub fn local_safe_live_account_id() -> Option<String> {
    let snapshot = load_local_oauth_snapshot_from_official_store(&get_codex_home())?;
    list_accounts()
        .into_iter()
        .find(|account| local_oauth_snapshot_matches_account(&snapshot, account))
        .map(|account| account.id)
}
