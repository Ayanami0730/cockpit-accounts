use base64::Engine;
use rand::Rng;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::future::Future;
use std::sync::{Arc, Mutex};
use tokio::sync::Notify;

use crate::models::cursor::CursorImportPayload;
use crate::modules::logger;

const CURSOR_LOGIN_URL: &str = "https://cursor.com/loginDeepControl";
const CURSOR_POLL_ENDPOINT: &str = "https://api2.cursor.sh/auth/poll";
const OAUTH_POLL_INTERVAL_MS: u64 = 2000;
const OAUTH_MAX_POLLS: u32 = 150;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CursorOAuthStartResponse {
    pub login_id: String,
    pub verification_uri: String,
    pub expires_in: u64,
    pub interval_seconds: u64,
}

#[derive(Debug, Clone)]
struct PendingOAuthState {
    login_id: String,
    uuid: String,
    code_verifier: String,
    expires_at: i64,
    cancelled: bool,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct PollResponse {
    access_token: Option<String>,
    refresh_token: Option<String>,
    auth_id: Option<String>,
}

lazy_static::lazy_static! {
    static ref PENDING_OAUTH_STATE: Arc<Mutex<Option<PendingOAuthState>>> = Arc::new(Mutex::new(None));
    static ref OAUTH_STATE_CHANGED: Notify = Notify::new();
}

fn now_timestamp() -> i64 {
    chrono::Utc::now().timestamp()
}

fn require_active_state(state: Option<&PendingOAuthState>, login_id: &str) -> Result<(), String> {
    let state = state.ok_or_else(|| "登录已取消或已结束".to_string())?;
    if state.login_id != login_id {
        return Err("登录流程已被新的授权替换".to_string());
    }
    if state.cancelled {
        return Err("登录已取消".to_string());
    }
    if now_timestamp() >= state.expires_at {
        return Err("登录会话已过期".to_string());
    }
    Ok(())
}

fn active_state(
    pending: &Mutex<Option<PendingOAuthState>>,
    login_id: &str,
) -> Result<PendingOAuthState, String> {
    let state = pending
        .lock()
        .map_err(|_| "获取 OAuth 状态锁失败".to_string())?;
    require_active_state(state.as_ref(), login_id)?;
    state
        .clone()
        .ok_or_else(|| "登录已取消或已结束".to_string())
}

// Register cancellation before checking state, so a cancellation between the
// check and select cannot be missed. Dropping the future also stops an in-flight
// request/body read; every completed response is checked again before use.
async fn while_login_active<T>(
    pending: &Mutex<Option<PendingOAuthState>>,
    changed: &Notify,
    login_id: &str,
    operation: impl Future<Output = T>,
) -> Result<T, String> {
    tokio::pin!(operation);
    loop {
        let notified = changed.notified();
        tokio::pin!(notified);
        notified.as_mut().enable();
        active_state(pending, login_id)?;
        tokio::select! {
            result = &mut operation => {
                active_state(pending, login_id)?;
                return Ok(result);
            }
            _ = &mut notified => {}
        }
    }
}

fn finish_login_in_state<T>(
    pending: &Mutex<Option<PendingOAuthState>>,
    login_id: &str,
    save: impl FnOnce() -> Result<T, String>,
) -> Result<T, String> {
    let mut state = pending
        .lock()
        .map_err(|_| "获取 OAuth 状态锁失败".to_string())?;
    require_active_state(state.as_ref(), login_id)?;
    // Saving and cancellation share this lock: a cancelled flow cannot save,
    // and cancelling after a completed save cannot revoke that completed login.
    let result = save()?;
    *state = None;
    Ok(result)
}

pub fn finish_login<T>(
    login_id: &str,
    save: impl FnOnce() -> Result<T, String>,
) -> Result<T, String> {
    let result = finish_login_in_state(&PENDING_OAUTH_STATE, login_id, save);
    OAUTH_STATE_CHANGED.notify_waiters();
    result
}

fn generate_code_verifier() -> String {
    let mut rng = rand::thread_rng();
    let bytes: Vec<u8> = (0..32).map(|_| rng.gen::<u8>()).collect();
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes)
}

fn generate_code_challenge(code_verifier: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(code_verifier.as_bytes());
    let digest = hasher.finalize();
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(digest)
}

fn generate_uuid() -> String {
    uuid::Uuid::new_v4().to_string()
}

pub fn start_login() -> Result<CursorOAuthStartResponse, String> {
    let code_verifier = generate_code_verifier();
    let code_challenge = generate_code_challenge(&code_verifier);
    let login_uuid = generate_uuid();
    let login_id = login_uuid.clone();

    let verification_uri = format!(
        "{}?challenge={}&uuid={}&mode=login",
        CURSOR_LOGIN_URL, code_challenge, login_uuid
    );

    let now = now_timestamp();
    let state = PendingOAuthState {
        login_id: login_id.clone(),
        uuid: login_uuid,
        code_verifier,
        expires_at: now + 300,
        cancelled: false,
    };

    *PENDING_OAUTH_STATE
        .lock()
        .map_err(|_| "获取 OAuth 状态锁失败".to_string())? = Some(state);
    OAUTH_STATE_CHANGED.notify_waiters();

    logger::log_info(&format!(
        "[Cursor OAuth] 登录会话已创建: login_id={}",
        login_id
    ));

    Ok(CursorOAuthStartResponse {
        login_id,
        verification_uri,
        expires_in: 300,
        interval_seconds: 2,
    })
}

pub async fn complete_login(login_id: &str) -> Result<CursorImportPayload, String> {
    let state = active_state(&PENDING_OAUTH_STATE, login_id)?;
    let (uuid, code_verifier) = (state.uuid, state.code_verifier);

    logger::log_info(&format!(
        "[Cursor OAuth] 开始轮询: login_id={}, uuid={}",
        login_id, uuid
    ));

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(15))
        .build()
        .map_err(|e| format!("创建 HTTP 客户端失败: {}", e))?;

    // CURSOR_POLL_ENDPOINT 使用 HTTPS（https://api2.cursor.sh），code_verifier 已通过 TLS 加密传输
    // lgtm[rs/cleartext-transmission] 实际通过 HTTPS 传输，非明文
    let poll_url = format!(
        "{}?uuid={}&verifier={}",
        CURSOR_POLL_ENDPOINT, uuid, code_verifier
    );

    for attempt in 0..OAUTH_MAX_POLLS {
        let response = while_login_active(
            &PENDING_OAUTH_STATE,
            &OAUTH_STATE_CHANGED,
            login_id,
            client
                .get(&poll_url)
                .header("Accept", "application/json")
                .send(),
        )
        .await?;

        match response {
            Ok(resp) => {
                let status = resp.status().as_u16();

                if status == 404 {
                    if attempt % 15 == 0 {
                        logger::log_info(&format!(
                            "[Cursor OAuth] 轮询中，等待用户完成登录... (attempt={})",
                            attempt
                        ));
                    }
                    wait_before_next_poll(login_id, OAUTH_POLL_INTERVAL_MS).await?;
                    continue;
                }

                if status != 200 {
                    logger::log_warn(&format!("[Cursor OAuth] 轮询返回异常状态码: {}", status));
                    wait_before_next_poll(login_id, OAUTH_POLL_INTERVAL_MS).await?;
                    continue;
                }

                let body = while_login_active(
                    &PENDING_OAUTH_STATE,
                    &OAUTH_STATE_CHANGED,
                    login_id,
                    resp.text(),
                )
                .await?
                .map_err(|e| format!("读取轮询响应失败: {}", e))?;

                let poll_data: PollResponse =
                    serde_json::from_str(&body).map_err(|e| format!("解析轮询响应失败: {}", e))?;

                if let (Some(access_token), Some(refresh_token)) =
                    (poll_data.access_token, poll_data.refresh_token)
                {
                    active_state(&PENDING_OAUTH_STATE, login_id)?;
                    logger::log_info("[Cursor OAuth] 已收到授权结果，等待保存前校验");

                    let email = poll_data
                        .auth_id
                        .as_deref()
                        .filter(|value| value.contains('@'))
                        .unwrap_or("")
                        .to_string();

                    let mut auth_raw = serde_json::Map::new();
                    auth_raw.insert(
                        "accessToken".to_string(),
                        serde_json::Value::String(access_token.clone()),
                    );
                    auth_raw.insert(
                        "refreshToken".to_string(),
                        serde_json::Value::String(refresh_token.clone()),
                    );
                    if let Some(ref auth_id) = poll_data.auth_id {
                        auth_raw.insert(
                            "authId".to_string(),
                            serde_json::Value::String(auth_id.clone()),
                        );
                    }

                    return Ok(CursorImportPayload {
                        email,
                        auth_id: poll_data.auth_id.clone(),
                        name: None,
                        access_token,
                        refresh_token: Some(refresh_token),
                        membership_type: None,
                        subscription_status: None,
                        sign_up_type: None,
                        cursor_auth_raw: Some(serde_json::Value::Object(auth_raw)),
                        cursor_usage_raw: None,
                        status: None,
                        status_reason: None,
                    });
                }

                logger::log_warn("[Cursor OAuth] 轮询成功但响应缺少 token");
                wait_before_next_poll(login_id, OAUTH_POLL_INTERVAL_MS).await?;
            }
            Err(err) => {
                logger::log_warn(&format!("[Cursor OAuth] 轮询请求失败: {}, 将重试", err));
                wait_before_next_poll(login_id, OAUTH_POLL_INTERVAL_MS * 2).await?;
            }
        }
    }

    cancel_login(Some(login_id))?;

    Err("Cursor 登录轮询超时，请重试".to_string())
}

async fn wait_before_next_poll(login_id: &str, milliseconds: u64) -> Result<(), String> {
    while_login_active(
        &PENDING_OAUTH_STATE,
        &OAUTH_STATE_CHANGED,
        login_id,
        tokio::time::sleep(std::time::Duration::from_millis(milliseconds)),
    )
    .await
}

fn cancel_login_in_state(
    pending: &Mutex<Option<PendingOAuthState>>,
    login_id: Option<&str>,
) -> Result<bool, String> {
    let mut pending = pending
        .lock()
        .map_err(|_| "获取 OAuth 状态锁失败".to_string())?;
    let matches = pending
        .as_ref()
        .is_some_and(|state| login_id.is_none() || login_id == Some(state.login_id.as_str()));
    if matches {
        *pending = None;
    }
    Ok(matches)
}

pub fn cancel_login(login_id: Option<&str>) -> Result<(), String> {
    if cancel_login_in_state(&PENDING_OAUTH_STATE, login_id)? {
        OAUTH_STATE_CHANGED.notify_waiters();
        logger::log_info("[Cursor OAuth] 登录已取消");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

    fn flow(login_id: &str) -> PendingOAuthState {
        PendingOAuthState {
            login_id: login_id.into(),
            uuid: "synthetic-request".into(),
            code_verifier: "synthetic-verifier".into(),
            expires_at: now_timestamp() + 300,
            cancelled: false,
        }
    }

    #[tokio::test]
    async fn oauth_cancel_missing_or_replaced_flow_never_polls() {
        for state in [None, Some(flow("new-flow"))] {
            let pending = Mutex::new(state);
            let changed = Notify::new();
            let polls = AtomicUsize::new(0);
            let result = while_login_active(&pending, &changed, "old-flow", async {
                polls.fetch_add(1, Ordering::SeqCst);
                "synthetic response"
            })
            .await;
            assert!(result.is_err());
            assert_eq!(polls.load(Ordering::SeqCst), 0);
        }
    }

    #[tokio::test]
    async fn oauth_cancel_interrupts_an_in_flight_fake_poll() {
        let pending = Arc::new(Mutex::new(Some(flow("flow"))));
        let changed = Arc::new(Notify::new());
        let (started, ready) = tokio::sync::oneshot::channel();
        let task = {
            let pending = pending.clone();
            let changed = changed.clone();
            tokio::spawn(async move {
                while_login_active(&pending, &changed, "flow", async {
                    started.send(()).unwrap();
                    std::future::pending::<()>().await;
                })
                .await
            })
        };
        ready.await.unwrap();
        assert!(cancel_login_in_state(&pending, Some("flow")).unwrap());
        changed.notify_waiters();
        let result = tokio::time::timeout(std::time::Duration::from_secs(1), task)
            .await
            .expect("cancellation must wake a pending request")
            .expect("fake poll task");
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn oauth_cancel_rechecks_a_ready_fake_response() {
        let pending = Mutex::new(Some(flow("flow")));
        let changed = Notify::new();
        let result = while_login_active(&pending, &changed, "flow", async {
            // Model cancellation after the request was sent but before its
            // response can be accepted by the poller.
            cancel_login_in_state(&pending, Some("flow")).unwrap();
            "synthetic successful authorization response"
        })
        .await;
        assert!(result.is_err());
    }

    #[test]
    fn oauth_cancel_between_response_and_save_prevents_persistence() {
        let pending = Mutex::new(Some(flow("flow")));
        active_state(&pending, "flow").expect("response was accepted while active");
        cancel_login_in_state(&pending, Some("flow")).unwrap();
        let saved = AtomicBool::new(false);
        let result = finish_login_in_state(&pending, "flow", || {
            saved.store(true, Ordering::SeqCst);
            Ok(())
        });
        assert!(result.is_err());
        assert!(!saved.load(Ordering::SeqCst));
    }

    #[test]
    fn oauth_cancel_stale_flow_cannot_cancel_or_save_over_new_flow() {
        let pending = Mutex::new(Some(flow("new-flow")));
        assert!(!cancel_login_in_state(&pending, Some("old-flow")).unwrap());
        let saved = AtomicBool::new(false);
        assert!(finish_login_in_state(&pending, "old-flow", || {
            saved.store(true, Ordering::SeqCst);
            Ok(())
        })
        .is_err());
        assert!(!saved.load(Ordering::SeqCst));
        assert!(active_state(&pending, "new-flow").is_ok());
    }

    #[test]
    fn oauth_cancel_expired_flow_cannot_save_and_active_flow_saves_once() {
        let mut expired = flow("expired");
        expired.expires_at = now_timestamp();
        let pending = Mutex::new(Some(expired));
        assert!(finish_login_in_state(&pending, "expired", || Ok(())).is_err());

        *pending.lock().unwrap() = Some(flow("active"));
        let saves = AtomicUsize::new(0);
        finish_login_in_state(&pending, "active", || {
            saves.fetch_add(1, Ordering::SeqCst);
            Ok(())
        })
        .unwrap();
        assert!(finish_login_in_state(&pending, "active", || {
            saves.fetch_add(1, Ordering::SeqCst);
            Ok(())
        })
        .is_err());
        assert_eq!(saves.load(Ordering::SeqCst), 1);
    }
}
