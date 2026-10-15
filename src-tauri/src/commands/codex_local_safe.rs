//! Minimal Codex account management for the local-safe build.
//!
//! Only the official credential store (`auth.json`) is written. `config.toml`, session
//! databases, instance settings and the Codex app bundle are never touched.

use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::AppHandle;
use tokio::process::Command;
use tokio::sync::Mutex;

use crate::models::codex::{CodexAccount, CodexQuota};
use crate::modules::{codex_account, codex_oauth, codex_quota, logger};

const CODEX_BUNDLE_ID: &str = "com.openai.codex";
const QUIT_TIMEOUT: Duration = Duration::from_secs(20);

static SWITCH_LOCK: Mutex<()> = Mutex::const_new(());

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LocalSafeCodexAccount {
    pub id: String,
    pub email: String,
    pub plan_type: Option<String>,
    pub subscription_active_until: Option<String>,
    pub quota: Option<CodexQuota>,
    pub usage_credits: Option<LocalSafeUsageCredits>,
    pub quota_error: Option<String>,
    pub usage_updated_at: Option<i64>,
    pub requires_reauth: bool,
    pub reauth_reason: Option<String>,
    pub access_token_expires_at: Option<i64>,
    pub has_refresh_token: bool,
    pub is_current: bool,
    pub is_live_local: bool,
    pub last_used: i64,
    pub created_at: i64,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LocalSafeRemoteResult {
    pub host: String,
    pub ok: bool,
    pub message: String,
    pub running_app_servers: Option<u32>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LocalSafeSwitchResult {
    pub account: LocalSafeCodexAccount,
    pub codex_was_running: bool,
    pub relaunched: bool,
    pub other_codex_processes: u32,
    pub remote: Option<LocalSafeRemoteResult>,
    pub notes: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LocalSafeRemoteStatus {
    pub host: String,
    pub reachable: bool,
    pub message: String,
    pub account_id: Option<String>,
    pub email: Option<String>,
    pub running_app_servers: Option<u32>,
}

/// Usage-credit balance (the purchasable Codex/Work credits, not rate-limit reset credits).
#[derive(Debug, Clone, Serialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct LocalSafeUsageCredits {
    pub has_credits: bool,
    pub unlimited: bool,
    pub balance: Option<f64>,
}

/// Reads `credits` from the stored wham/usage body. The official client types `balance` as a
/// decimal string; numbers are accepted too.
fn usage_credits_from_raw(raw: Option<&serde_json::Value>) -> Option<LocalSafeUsageCredits> {
    let credits = raw?.get("credits")?.as_object()?;
    let balance = match credits.get("balance") {
        Some(serde_json::Value::String(text)) => text.trim().replace(',', "").parse::<f64>().ok(),
        Some(serde_json::Value::Number(number)) => number.as_f64(),
        _ => None,
    };
    Some(LocalSafeUsageCredits {
        has_credits: credits.get("has_credits").and_then(|v| v.as_bool()).unwrap_or(false),
        unlimited: credits.get("unlimited").and_then(|v| v.as_bool()).unwrap_or(false),
        balance,
    })
}

fn summarize(account: &CodexAccount, current_id: Option<&str>, live_id: Option<&str>) -> LocalSafeCodexAccount {
    let mut quota = account.quota.clone();
    let usage_credits = usage_credits_from_raw(quota.as_ref().and_then(|q| q.raw_data.as_ref()));
    if let Some(quota) = quota.as_mut() {
        quota.raw_data = None;
    }
    LocalSafeCodexAccount {
        id: account.id.clone(),
        email: account.email.clone(),
        plan_type: account.plan_type.clone(),
        subscription_active_until: account.subscription_active_until.clone(),
        quota,
        usage_credits,
        quota_error: account.quota_error.as_ref().map(|error| error.message.clone()),
        usage_updated_at: account.usage_updated_at,
        requires_reauth: account.requires_reauth,
        reauth_reason: account.reauth_reason.clone(),
        access_token_expires_at: codex_oauth::jwt_token_expiration_timestamp(
            &account.tokens.access_token,
        ),
        has_refresh_token: codex_account::account_has_refresh_token(account),
        is_current: current_id == Some(account.id.as_str()),
        is_live_local: live_id == Some(account.id.as_str()),
        last_used: account.last_used,
        created_at: account.created_at,
    }
}

fn summarize_all() -> Vec<LocalSafeCodexAccount> {
    let current_id = codex_account::load_account_index().current_account_id;
    let live_id = codex_account::local_safe_live_account_id();
    codex_account::list_accounts()
        .iter()
        .filter(|account| {
            !account.is_api_key_auth()
                && !account.is_agent_identity_auth()
                && !account.is_web_session_auth()
        })
        .map(|account| summarize(account, current_id.as_deref(), live_id.as_deref()))
        .collect()
}

fn summarize_one(account_id: &str) -> Result<LocalSafeCodexAccount, String> {
    summarize_all()
        .into_iter()
        .find(|account| account.id == account_id)
        .ok_or_else(|| format!("账号不存在: {}", account_id))
}

fn adopt_local_credentials() -> Option<String> {
    match codex_account::local_safe_adopt_credentials_from_codex_home(
        &codex_account::get_codex_home(),
    ) {
        Ok(adoption) => adoption.map(|adoption| adoption.account_id),
        Err(error) => {
            logger::log_warn(&format!("[Codex Local] 回读本机 Codex 凭据失败: {}", error));
            None
        }
    }
}

/// Accounts whose credentials are held by a running Codex somewhere. The manager must not
/// rotate their refresh tokens, or the copy Codex holds stops working.
fn is_in_use(account_id: &str, live_id: Option<&str>) -> bool {
    live_id == Some(account_id)
        || codex_account::load_account_index().current_account_id.as_deref() == Some(account_id)
}

fn ensure_in_use_account_not_expired(account_id: &str, in_use: bool) -> Result<(), String> {
    if !in_use {
        return Ok(());
    }
    let account = codex_account::load_account(account_id)
        .ok_or_else(|| format!("账号不存在: {}", account_id))?;
    if codex_oauth::is_token_expired(&account.tokens.access_token) {
        return Err("这个账号正在 Codex 里使用，而它的 access token 已过期。请先在 Codex 里发一条消息让它自己刷新，再回来操作。登号器不替正在使用的账号刷新凭据，否则 Codex 手里那份会失效。".to_string());
    }
    Ok(())
}

/// If an upstream call rotated the tokens of an in-use account anyway (for example after a
/// 401), hand the new tokens to the local Codex home so its copy stays valid.
fn write_back_if_rotated(
    account_id: &str,
    live_id: Option<&str>,
    generation_before: u64,
) -> Option<String> {
    if live_id != Some(account_id) {
        return None;
    }
    let account = codex_account::load_account(account_id)?;
    if account.token_generation <= generation_before {
        return None;
    }
    match codex_account::local_safe_write_credentials(&codex_account::get_codex_home(), &account) {
        Ok(_) => Some("已替当前账号刷新了凭据并写回本机 Codex。".to_string()),
        Err(error) => Some(format!("当前账号凭据已刷新，但写回本机 Codex 失败: {}", error)),
    }
}

fn generation_of(account_id: &str) -> u64 {
    codex_account::load_account(account_id)
        .map(|account| account.token_generation)
        .unwrap_or(0)
}

async fn run_osascript(script: &str) -> Result<String, String> {
    let output = Command::new("/usr/bin/osascript")
        .args(["-e", script])
        .output()
        .await
        .map_err(|error| format!("osascript 执行失败: {}", error))?;
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).trim().to_string());
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

async fn codex_app_running() -> bool {
    run_osascript(&format!("application id \"{}\" is running", CODEX_BUNDLE_ID))
        .await
        .map(|value| value == "true")
        .unwrap_or(false)
}

async fn quit_codex_app() -> Result<(), String> {
    run_osascript(&format!("tell application id \"{}\" to quit", CODEX_BUNDLE_ID))
        .await
        .map_err(|error| format!("请求 Codex 退出失败: {}", error))?;
    let started = Instant::now();
    while started.elapsed() < QUIT_TIMEOUT {
        if !codex_app_running().await {
            return Ok(());
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }
    Err("Codex 没有在 20 秒内退出（可能还有任务在跑，或弹出了确认框）。已取消切换，账号没有改动。".to_string())
}

async fn launch_codex_app() -> Result<(), String> {
    let status = Command::new("/usr/bin/open")
        .args(["-b", CODEX_BUNDLE_ID])
        .status()
        .await
        .map_err(|error| format!("启动 Codex 失败: {}", error))?;
    if status.success() {
        Ok(())
    } else {
        Err(format!("启动 Codex 失败: exit={}", status))
    }
}

/// Codex CLI processes outside the desktop app (terminals, editor extensions) keep the old
/// credentials in memory until restarted.
async fn count_other_codex_processes() -> u32 {
    let Ok(output) = Command::new("/bin/ps").args(["-axo", "command"]).output().await else {
        return 0;
    };
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter(|line| {
            let executable = line.split_whitespace().next().unwrap_or("");
            executable.rsplit('/').next() == Some("codex")
                && !line.contains("/Applications/ChatGPT.app/")
        })
        .count() as u32
}

#[tauri::command]
pub fn local_safe_codex_notify(app: AppHandle, title: String, body: String) -> Result<(), String> {
    use tauri_plugin_notification::NotificationExt;
    app.notification()
        .builder()
        .title(title)
        .body(body)
        .show()
        .map_err(|error| format!("发送系统通知失败: {}", error))
}

#[tauri::command]
pub async fn local_safe_codex_list() -> Result<Vec<LocalSafeCodexAccount>, String> {
    tokio::task::spawn_blocking(|| {
        adopt_local_credentials();
        summarize_all()
    })
    .await
    .map_err(|error| error.to_string())
}

#[tauri::command]
pub async fn local_safe_codex_import_local() -> Result<LocalSafeCodexAccount, String> {
    let account = codex_account::import_from_local()?;
    if let Err(error) = codex_quota::refresh_account_quota(&account.id).await {
        logger::log_warn(&format!("[Codex Local] 导入后刷新额度失败: {}", error));
    }
    summarize_one(&account.id)
}

#[tauri::command]
pub async fn local_safe_codex_oauth_start(
    app: AppHandle,
) -> Result<codex_oauth::CodexOAuthLoginStartResponse, String> {
    codex_oauth::start_oauth_login(app).await
}

#[tauri::command]
pub async fn local_safe_codex_oauth_complete(
    login_id: String,
) -> Result<LocalSafeCodexAccount, String> {
    let tokens = codex_oauth::complete_oauth_login(&login_id).await?;
    let account = codex_account::upsert_account(tokens)?;
    if let Err(error) = codex_quota::refresh_account_quota(&account.id).await {
        logger::log_warn(&format!("[Codex Local] 登录后刷新额度失败: {}", error));
    }
    summarize_one(&account.id)
}

#[tauri::command]
pub fn local_safe_codex_oauth_cancel(login_id: Option<String>) -> Result<(), String> {
    codex_oauth::cancel_oauth_flow_for(login_id.as_deref())
}

async fn refresh_quota_guarded(account_id: &str) -> Result<Option<String>, String> {
    let live_id = adopt_local_credentials();
    let in_use = is_in_use(account_id, live_id.as_deref());
    ensure_in_use_account_not_expired(account_id, in_use)?;
    let generation = generation_of(account_id);
    let result = codex_quota::refresh_account_quota(account_id).await;
    let note = write_back_if_rotated(account_id, live_id.as_deref(), generation);
    result.map(|_| note)
}

#[tauri::command]
pub async fn local_safe_codex_refresh_quota(
    account_id: String,
) -> Result<LocalSafeCodexAccount, String> {
    refresh_quota_guarded(&account_id).await?;
    summarize_one(&account_id)
}

#[tauri::command]
pub async fn local_safe_codex_refresh_all() -> Result<Vec<LocalSafeCodexAccount>, String> {
    let ids: Vec<String> = summarize_all().into_iter().map(|account| account.id).collect();
    for id in ids {
        if let Err(error) = refresh_quota_guarded(&id).await {
            logger::log_warn(&format!(
                "[Codex Local] 刷新额度失败: {}",
                error
            ));
        }
    }
    Ok(summarize_all())
}

#[tauri::command]
pub async fn local_safe_codex_reset_credits(account_id: String) -> Result<serde_json::Value, String> {
    let live_id = adopt_local_credentials();
    let in_use = is_in_use(&account_id, live_id.as_deref());
    ensure_in_use_account_not_expired(&account_id, in_use)?;
    let generation = generation_of(&account_id);
    let result = codex_quota::fetch_account_reset_credits(&account_id).await;
    write_back_if_rotated(&account_id, live_id.as_deref(), generation);
    serde_json::to_value(result?).map_err(|error| error.to_string())
}

#[tauri::command]
pub async fn local_safe_codex_consume_reset_credit(
    account_id: String,
) -> Result<LocalSafeCodexAccount, String> {
    let live_id = adopt_local_credentials();
    let in_use = is_in_use(&account_id, live_id.as_deref());
    ensure_in_use_account_not_expired(&account_id, in_use)?;
    let generation = generation_of(&account_id);
    let result = codex_quota::consume_reset_credit(&account_id).await;
    write_back_if_rotated(&account_id, live_id.as_deref(), generation);
    result?;
    logger::log_info("[Codex Local] 已使用一次额度重置");
    if let Err(error) = refresh_quota_guarded(&account_id).await {
        logger::log_warn(&format!("[Codex Local] 重置后刷新额度失败: {}", error));
    }
    summarize_one(&account_id)
}

#[tauri::command]
pub fn local_safe_codex_delete(account_id: String) -> Result<(), String> {
    if codex_account::local_safe_live_account_id().as_deref() == Some(account_id.as_str()) {
        return Err("这个账号正在本机 Codex 中使用，先切到别的账号再删除。".to_string());
    }
    codex_account::remove_account(&account_id)
}

#[tauri::command]
pub async fn local_safe_codex_switch(
    account_id: String,
    relaunch: bool,
) -> Result<LocalSafeSwitchResult, String> {
    let _guard = SWITCH_LOCK.try_lock().map_err(|_| "已有一次切换在进行".to_string())?;
    let mut notes = Vec::new();

    let target = codex_account::load_account(&account_id)
        .ok_or_else(|| format!("账号不存在: {}", account_id))?;
    codex_account::local_safe_auth_json_value(&target)?;

    // Save the outgoing account's latest (possibly rotated) credentials before anything else.
    let live_before = adopt_local_credentials();

    if !is_in_use(&account_id, live_before.as_deref()) {
        if codex_oauth::is_token_expired(&target.tokens.access_token) {
            codex_account::prepare_account_for_quota_query(&account_id)
                .await
                .map_err(|error| format!("目标账号凭据已过期且刷新失败: {}", error))?;
            notes.push("目标账号的 access token 已过期，切换前已刷新。".to_string());
        }
    } else {
        ensure_in_use_account_not_expired(&account_id, true)?;
    }

    let codex_was_running = codex_app_running().await;
    if codex_was_running {
        quit_codex_app().await?;
        // Codex may persist refreshed credentials while shutting down.
        adopt_local_credentials();
    }

    let target = codex_account::load_account(&account_id)
        .ok_or_else(|| format!("账号不存在: {}", account_id))?;
    let _store = codex_account::local_safe_write_credentials(&codex_account::get_codex_home(), &target)?;
    codex_account::local_safe_mark_current(&account_id)?;
    logger::log_info("[Codex Local] 已切换本机 Codex 账号");

    let remote = None;

    let mut relaunched = false;
    if relaunch {
        match launch_codex_app().await {
            Ok(()) => relaunched = true,
            Err(error) => notes.push(error),
        }
    }

    let other_codex_processes = count_other_codex_processes().await;
    if other_codex_processes > 0 {
        notes.push(format!(
            "还有 {} 个命令行 Codex 进程（终端或编辑器扩展）在用旧账号，重启它们后才会换号。",
            other_codex_processes
        ));
    }

    Ok(LocalSafeSwitchResult {
        account: summarize_one(&account_id)?,
        codex_was_running,
        relaunched,
        other_codex_processes,
        remote,
        notes,
    })
}

#[cfg(test)]
mod local_safe_usage_credit_tests {
    use super::*;

    #[test]
    fn local_safe_reads_usage_credit_balance() {
        let raw = serde_json::json!({
            "plan_type": "pro",
            "credits": { "has_credits": true, "unlimited": false, "balance": "62500" }
        });
        assert_eq!(
            usage_credits_from_raw(Some(&raw)),
            Some(LocalSafeUsageCredits { has_credits: true, unlimited: false, balance: Some(62500.0) })
        );

        let numeric = serde_json::json!({ "credits": { "has_credits": true, "unlimited": false, "balance": 9.99 } });
        assert_eq!(usage_credits_from_raw(Some(&numeric)).and_then(|c| c.balance), Some(9.99));
    }

    #[test]
    fn local_safe_usage_credits_absent_or_null() {
        assert_eq!(usage_credits_from_raw(None), None);
        assert_eq!(usage_credits_from_raw(Some(&serde_json::json!({ "plan_type": "plus" }))), None);
        let null_balance = serde_json::json!({ "credits": { "has_credits": false, "unlimited": false, "balance": null } });
        assert_eq!(
            usage_credits_from_raw(Some(&null_balance)),
            Some(LocalSafeUsageCredits { has_credits: false, unlimited: false, balance: None })
        );
    }
}
