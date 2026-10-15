//! Explicit IPC surface for the independent Claude companion. No legacy Claude commands.
use crate::modules::claude_local_safe::{
    Action, ClaudeActionResult, ClaudeOverview, ClaudePreflight, Runner, SessionBackup,
    SessionInventory, Store,
};
use tauri::AppHandle;

async fn blocking<T, F>(operation: F) -> Result<T, String>
where
    T: Send + 'static,
    F: FnOnce() -> Result<T, String> + Send + 'static,
{
    tokio::task::spawn_blocking(operation)
        .await
        .map_err(|_| "Claude 本地操作未完成。".to_string())?
}

#[tauri::command]
pub async fn local_safe_claude_list(app: AppHandle) -> Result<ClaudeOverview, String> {
    blocking(move || Runner::for_app(&app)?.cached_overview(&Store::production()?)).await
}

#[tauri::command]
pub async fn local_safe_claude_archive_profile(app: AppHandle, profile_id: String, archived: bool) -> Result<ClaudeActionResult, String> {
    blocking(move || Runner::for_app(&app)?.archive_profile(&Store::production()?, &profile_id, archived)).await
}

#[tauri::command]
pub async fn local_safe_claude_switch(app: AppHandle, action: String, source_id: Option<String>, target_id: Option<String>,
                                    operation_id: Option<String>, confirmed: Option<bool>) -> Result<serde_json::Value, String> {
    blocking(move || Runner::for_app(&app)?.switch_operation(&Store::production()?, &action, source_id.as_deref(),
        target_id.as_deref(), operation_id.as_deref(), confirmed.unwrap_or(false))).await
}

#[tauri::command]
pub async fn local_safe_claude_record_health(app: AppHandle, profile_id: String, status: String, reason: String,
                                           source: String, observed_at: i64) -> Result<ClaudeOverview, String> {
    blocking(move || Runner::for_app(&app)?.record_health(&Store::production()?, &profile_id, &status, &reason, &source, observed_at)).await
}

#[tauri::command]
pub async fn local_safe_claude_inspect_auth(
    app: AppHandle,
    profile_id: String,
) -> Result<ClaudeOverview, String> {
    blocking(move || Runner::for_app(&app)?.inspect_auth(&Store::production()?, &profile_id)).await
}

#[tauri::command]
pub async fn local_safe_claude_sessions(
    app: AppHandle,
    profile_id: String,
) -> Result<SessionInventory, String> {
    blocking(move || Runner::for_app(&app)?.sessions(&Store::production()?, &profile_id)).await
}

#[tauri::command]
pub async fn local_safe_claude_snapshot(
    app: AppHandle,
    profile_id: String,
) -> Result<SessionBackup, String> {
    blocking(move || Runner::for_app(&app)?.backup_sessions(&Store::production()?, &profile_id))
        .await
}

#[tauri::command]
pub async fn local_safe_claude_resume(
    app: AppHandle,
    source_profile_id: String,
    target_profile_id: String,
    session_id: String,
) -> Result<ClaudeActionResult, String> {
    blocking(move || {
        Runner::for_app(&app)?.resume_session(
            &Store::production()?,
            &source_profile_id,
            &target_profile_id,
            &session_id,
        )
    })
    .await
}

#[tauri::command]
pub async fn local_safe_claude_create(label: String) -> Result<ClaudeOverview, String> {
    blocking(move || {
        let store = Store::production()?;
        store.create(label)?;
        store.overview()
    })
    .await
}

#[tauri::command]
pub async fn local_safe_claude_register_current() -> Result<ClaudeOverview, String> {
    blocking(|| {
        let store = Store::production()?;
        store.register_current()?;
        store.overview()
    })
    .await
}

#[tauri::command]
pub async fn local_safe_claude_select(profile_id: String) -> Result<ClaudeOverview, String> {
    blocking(move || {
        let store = Store::production()?;
        store.select(&profile_id)?;
        store.overview()
    })
    .await
}

#[tauri::command]
pub async fn local_safe_claude_preferences(
    profile_id: String,
    auto_refresh: bool,
    renewal_date: Option<String>,
) -> Result<ClaudeOverview, String> {
    blocking(move || {
        let store = Store::production()?;
        store.preferences(&profile_id, auto_refresh, renewal_date)?;
        store.overview()
    })
    .await
}

#[tauri::command]
pub async fn local_safe_claude_refresh(
    app: AppHandle,
    profile_id: String,
) -> Result<ClaudeOverview, String> {
    blocking(move || Runner::for_app(&app)?.refresh(&Store::production()?, &profile_id)).await
}

#[tauri::command]
pub async fn local_safe_claude_refresh_all(
    app: AppHandle,
    automatic: Option<bool>,
) -> Result<ClaudeOverview, String> {
    blocking(move || {
        Runner::for_app(&app)?.refresh_all(&Store::production()?, automatic.unwrap_or(false))
    })
    .await
}

#[tauri::command]
pub async fn local_safe_claude_preflight(app: AppHandle) -> Result<ClaudePreflight, String> {
    blocking(move || Runner::for_app(&app)?.preflight(&Store::production()?)).await
}

#[tauri::command]
pub async fn local_safe_claude_action(
    app: AppHandle,
    profile_id: String,
    action: String,
) -> Result<ClaudeActionResult, String> {
    let action = Action::from_user(&action)?;
    blocking(move || Runner::for_app(&app)?.action(&Store::production()?, &profile_id, action))
        .await
}

#[tauri::command]
pub fn local_safe_claude_notify(app: AppHandle, title: String, body: String) -> Result<(), String> {
    use tauri_plugin_notification::NotificationExt;
    if title.is_empty()
        || title.chars().count() > 100
        || body.chars().count() > 500
        || title.chars().any(char::is_control)
        || body.chars().any(char::is_control)
    {
        return Err("通知内容不符合要求。".into());
    }
    app.notification()
        .builder()
        .title(title)
        .body(body)
        .show()
        .map_err(|_| "系统通知未发送。".into())
}
