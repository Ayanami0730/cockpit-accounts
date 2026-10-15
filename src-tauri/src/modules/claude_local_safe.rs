//! Claude companion metadata and an explicitly bounded official-CLI helper.
//! This module never reads, imports, exports or writes Claude credentials.

use std::collections::HashSet;
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager};
use uuid::Uuid;

pub const CURRENT_PROFILE_ID: &str = "00000000-0000-4000-8000-000000000001";
pub const PYTHON_MISSING: &str = "CLAUDE_PYTHON_MISSING";
const HELPER_NAME: &str = "claude-local-safe-runner.py";
const MAX_JSON_BYTES: usize = 256 * 1024;
const MAX_PROFILES: usize = 100;
const STORAGE_ERROR: &str = "Claude 配置索引不可用或路径权限不安全；未修改官方登录。";
const HELPER_ERROR: &str = "Claude 本地助手未完成检查；未切换账号或输出诊断原文。";
const SNAPSHOT_ERROR: &str = "用量快照不可用，请手动刷新或查看官方用量页。";
static REGISTRY_LOCK: Mutex<()> = Mutex::new(());

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ProfileKind {
    Current,
    Managed,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum AuthStatus {
    Unknown,
    SignedIn,
    SignedOut,
    Error,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum UsageSource {
    OfficialCli,
    OfficialStatusline,
    DesktopCache,
    None,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum AuthSource {
    CachedMetadata,
    OfficialCli,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct WindowUsage {
    pub used_percent: f64,
    pub resets_at: Option<i64>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ExtraUsage {
    pub enabled: bool,
    pub used_minor: Option<f64>,
    pub limit_minor: Option<f64>,
    pub currency: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ClaudeUsage {
    pub five_hour: Option<WindowUsage>,
    pub seven_day: Option<WindowUsage>,
    pub extra_usage: Option<ExtraUsage>,
    pub observed_at: Option<i64>,
    pub source: UsageSource,
    pub error: Option<String>,
    #[serde(default)]
    pub error_code: Option<String>,
}

impl Default for ClaudeUsage {
    fn default() -> Self {
        Self {
            five_hour: None,
            seven_day: None,
            extra_usage: None,
            observed_at: None,
            source: UsageSource::None,
            error: None,
            error_code: None,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ClaudeAuth {
    pub status: AuthStatus,
    pub email: Option<String>,
    pub plan: Option<String>,
    pub checked_at: Option<i64>,
    #[serde(default)]
    pub source: Option<AuthSource>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ClaudeHealth {
    pub status: String,
    pub reason: Option<String>,
    pub source: String,
    pub observed_at: Option<i64>,
    pub last_successful_at: Option<i64>,
    pub last_attempt_at: Option<i64>,
    pub last_attempt_outcome: Option<String>,
}
impl Default for ClaudeHealth {
    fn default() -> Self {
        Self { status: "unknown".into(), reason: None, source: "none".into(), observed_at: None,
            last_successful_at: None, last_attempt_at: None, last_attempt_outcome: None }
    }
}

impl Default for ClaudeAuth {
    fn default() -> Self {
        Self {
            status: AuthStatus::Unknown,
            email: None,
            plan: None,
            checked_at: None,
            source: None,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct ProfileMetadata {
    id: String,
    label: String,
    kind: ProfileKind,
    config_dir: String,
    desktop_dir: String,
    created_at: i64,
    collector_installed: bool,
    auto_refresh: bool,
    renewal_date: Option<String>,
    #[serde(default)]
    archived: bool,
    #[serde(default)]
    archived_at: Option<i64>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct Registry {
    version: u32,
    selected_profile_id: Option<String>,
    profiles: Vec<ProfileMetadata>,
}

impl Default for Registry {
    fn default() -> Self {
        Self {
            version: 1,
            selected_profile_id: None,
            profiles: Vec::new(),
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct Snapshot {
    auth: ClaudeAuth,
    usage: ClaudeUsage,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ClaudeProfile {
    pub id: String,
    pub label: String,
    pub kind: ProfileKind,
    pub config_dir: String,
    pub desktop_dir: String,
    pub created_at: i64,
    pub selected: bool,
    pub collector_installed: bool,
    pub auto_refresh: bool,
    pub renewal_date: Option<String>,
    pub auth: ClaudeAuth,
    pub usage: ClaudeUsage,
    #[serde(default)]
    pub health: ClaudeHealth,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ClaudeOverview {
    pub profiles: Vec<ClaudeProfile>,
    #[serde(default)]
    pub archived_profiles: Vec<ArchivedClaudeProfile>,
    pub selected_profile_id: Option<String>,
    pub desktop_running: bool,
    pub code_process_count: u32,
    pub checked_at: i64,
    pub warning: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ArchivedClaudeProfile {
    pub id: String,
    pub label: String,
    pub kind: ProfileKind,
    pub archived_at: Option<i64>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ClaudeSession {
    pub session_id: String,
    pub label: String,
    pub cwd: Option<String>,
    pub project_key: String,
    pub updated_at: i64,
    pub active: bool,
    pub activity: String,
    pub parse_status: String,
    pub stable: bool,
    pub files: u64,
    pub bytes: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionSummary {
    pub files: u64,
    pub bytes: u64,
    pub jsonl_files: u64,
    pub incomplete_files: u64,
    pub invalid_files: u64,
    pub symlinks_skipped: u64,
    pub unstable_files: u64,
    pub unverified_files: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionInventory {
    pub checked_at: i64,
    pub profile_id: String,
    pub active: bool,
    pub activity_known: bool,
    pub sessions: Vec<ClaudeSession>,
    pub summary: SessionSummary,
    #[serde(default)]
    pub last_backup: Option<SessionBackup>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionBackup {
    pub ok: bool,
    pub backup_id: String,
    pub backup_path: String,
    pub files: u64,
    pub bytes: u64,
    pub unstable_files: u64,
    pub incomplete_files: u64,
    pub invalid_files: u64,
    pub receipt_path: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct PreflightCheck {
    pub label: String,
    pub ok: bool,
    pub detail: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ClaudePreflight {
    pub ok: bool,
    pub checked_at: i64,
    pub cli_path: Option<String>,
    pub cli_version: Option<String>,
    pub checks: Vec<PreflightCheck>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ClaudeActionResult {
    pub ok: bool,
    pub message: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub code: Option<String>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Action {
    LoginCli,
    LaunchCli,
    LaunchDesktop,
    OpenUsage,
    InstallCollector,
    Refresh,
    InspectAuth,
    Preflight,
}

impl Action {
    pub fn from_user(value: &str) -> Result<Self, String> {
        match value {
            "login_cli" => Ok(Self::LoginCli),
            "launch_cli" => Ok(Self::LaunchCli),
            "launch_desktop" => Ok(Self::LaunchDesktop),
            "open_usage" => Ok(Self::OpenUsage),
            "install_collector" => Ok(Self::InstallCollector),
            _ => Err("不支持此 Claude 操作。".into()),
        }
    }
    fn name(self) -> &'static str {
        match self {
            Self::LoginCli => "login_cli",
            Self::LaunchCli => "launch_cli",
            Self::LaunchDesktop => "launch_desktop",
            Self::OpenUsage => "open_usage",
            Self::InstallCollector => "install_collector",
            Self::Refresh => "refresh",
            Self::InspectAuth => "inspect_auth",
            Self::Preflight => "preflight",
        }
    }
    fn timeout(self) -> Duration {
        Duration::from_secs(match self {
            Self::Refresh => 90,
            Self::Preflight => 30,
            _ => 45,
        })
    }
    fn safe_result(self, ok: bool, code: Option<&str>) -> ClaudeActionResult {
        let safe_code = code.filter(|code| {
            matches!(
                *code,
                "desktop_busy"
                    | "collector_conflict"
                    | "proxy_unavailable"
                    | "auth_required"
                    | "unsupported_cli"
                    | "invalid_profile"
                    | "busy"
                    | "network_unavailable"
                    | "auth_expired"
                    | "account_disabled"
                    | "usage_unavailable"
                    | "identity_mismatch"
                    | "archived"
            )
        });
        let message = if !ok {
            match safe_code {
                Some("desktop_busy") => "Claude Desktop 或 Code 仍在运行；请等任务结束后再启动独立 Desktop，现有进程未被结束。",
                Some("collector_conflict") => "该配置已有自定义状态栏，未覆盖；请保留原设置并查看官方用量页。",
                Some("proxy_unavailable") => "官方客户端检查未通过；请检查 Claude Code 安装。",
                Some("auth_required") => "该配置尚未登录或登录已失效；请通过官方 CLI 完成登录。",
                Some("auth_expired") => "官方客户端明确报告登录已过期；请在方便时重新登录。",
                Some("account_disabled") => "官方客户端明确报告账号已停用或暂停；请到官方页面核对。",
                Some("usage_unavailable") => "官方客户端尚未提供可用的用量数据。",
                Some("identity_mismatch") => "CLI 与 Desktop 本地账号记录不一致；请核对各自账号。",
                Some("archived") => "该配置已从账号列表移除；请先恢复到列表。",
                Some("unsupported_cli") => "当前官方 CLI 版本或响应格式不支持此项采集；请查看官方用量页。",
                Some("invalid_profile") => "配置目录或权限核验失败；未改写登录资料。",
                Some("busy") => "上次官方查询因账号正在使用而跳过；保留原采样时间。",
                Some("network_unavailable") => "官方查询遇到网络错误或超时；这不代表账号被封禁。",
                _ => match self {
                    Self::InstallCollector => "状态栏采集未安装；请检查已有自定义状态栏及配置目录权限。",
                    Self::OpenUsage => "官方用量页未打开；请检查默认浏览器。",
                    _ => "操作未完成；请检查客户端安装、登录状态及该配置是否有正在运行的任务。",
                },
            }
        } else {
            match self {
                Self::LoginCli => "已打开官方 CLI 登录流程；完成后请刷新核对账号。",
                Self::LaunchCli => "已请求启动所选配置的官方 CLI，原有会话保持不变。",
                Self::LaunchDesktop => "已请求启动独立 Desktop 配置；请在官方界面自行登录。",
                Self::OpenUsage => "已打开官方用量页；请在网页内核对当前登录账号。",
                Self::InstallCollector => "已安装官方状态栏用量采集；不会为采集发送模型消息。",
                Self::Refresh => "已通过官方 CLI 查询状态；官方客户端可能续期自身登录。",
                Self::InspectAuth => "已读取当前账号的本地记录；未启动 CLI 或重新登录。",
                Self::Preflight => "已完成网络检查。",
            }
        };
        ClaudeActionResult {
            ok,
            message: message.into(),
            code: if ok {
                None
            } else {
                safe_code.map(str::to_string)
            },
        }
    }
}

fn now() -> i64 {
    chrono::Utc::now().timestamp()
}
fn fixed_error() -> String {
    STORAGE_ERROR.into()
}

fn validate_id(id: &str) -> Result<(), String> {
    let parsed = Uuid::parse_str(id).map_err(|_| fixed_error())?;
    if parsed.hyphenated().to_string() != id || parsed.is_nil() {
        return Err(fixed_error());
    }
    Ok(())
}

fn validate_label(label: &str) -> bool {
    !label.trim().is_empty() && label.chars().count() <= 80 && !label.chars().any(char::is_control)
}

fn validate_renewal(value: &Option<String>) -> bool {
    value
        .as_ref()
        .map(|value| {
            chrono::NaiveDate::parse_from_str(value, "%Y-%m-%d")
                .map(|date| date.format("%Y-%m-%d").to_string() == *value)
                .unwrap_or(false)
        })
        .unwrap_or(true)
}

fn owned_metadata(path: &Path) -> Result<Option<fs::Metadata>, String> {
    match fs::symlink_metadata(path) {
        Ok(metadata) => {
            #[cfg(unix)]
            {
                use std::os::unix::fs::MetadataExt;
                if metadata.file_type().is_symlink() || metadata.uid() != unsafe { libc::geteuid() }
                {
                    return Err(fixed_error());
                }
            }
            #[cfg(not(unix))]
            {
                return Err(fixed_error());
            }
            Ok(Some(metadata))
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(_) => Err(fixed_error()),
    }
}

/// Inspect only path metadata, never profile contents. Existing symlinks are rejected,
/// including intermediate directories. Native metadata directories are owner-only.
fn validate_path(
    home: &Path,
    native: &Path,
    path: &Path,
    private_file: bool,
) -> Result<(), String> {
    if !home.is_absolute()
        || !path.starts_with(home)
        || path
            .components()
            .any(|c| matches!(c, Component::ParentDir | Component::CurDir))
    {
        return Err(fixed_error());
    }
    let mut current = home.to_path_buf();
    if let Some(meta) = owned_metadata(&current)? {
        if !meta.is_dir() {
            return Err(fixed_error());
        }
    } else {
        return Err(fixed_error());
    }
    for part in path
        .strip_prefix(home)
        .map_err(|_| fixed_error())?
        .components()
    {
        if !matches!(part, Component::Normal(_)) {
            return Err(fixed_error());
        }
        current.push(part.as_os_str());
        if let Some(meta) = owned_metadata(&current)? {
            if current != path && !meta.is_dir() {
                return Err(fixed_error());
            }
            if current == path && !private_file && !meta.is_dir() {
                return Err(fixed_error());
            }
            #[cfg(unix)]
            {
                use std::os::unix::fs::MetadataExt;
                let mode = meta.mode() & 0o777;
                if mode & 0o022 != 0 || (current.starts_with(native) && mode & 0o077 != 0) {
                    return Err(fixed_error());
                }
                if current == path
                    && private_file
                    && (!meta.is_file() || mode & 0o077 != 0 || meta.nlink() != 1)
                {
                    return Err(fixed_error());
                }
            }
        }
    }
    Ok(())
}

fn private_dir(home: &Path, native: &Path, path: &Path) -> Result<(), String> {
    validate_path(home, native, path, false)?;
    let mut current = home.to_path_buf();
    for part in path
        .strip_prefix(home)
        .map_err(|_| fixed_error())?
        .components()
    {
        current.push(part.as_os_str());
        if owned_metadata(&current)?.is_none() {
            let mut builder = fs::DirBuilder::new();
            #[cfg(unix)]
            {
                use std::os::unix::fs::DirBuilderExt;
                builder.mode(0o700);
            }
            builder.create(&current).map_err(|_| fixed_error())?;
        }
    }
    validate_path(home, native, path, false)?;
    if !fs::symlink_metadata(path)
        .map_err(|_| fixed_error())?
        .is_dir()
    {
        return Err(fixed_error());
    }
    Ok(())
}

fn read_private_json<T: for<'de> Deserialize<'de>>(
    home: &Path,
    native: &Path,
    path: &Path,
) -> Result<Option<T>, String> {
    validate_path(home, native, path, true)?;
    if owned_metadata(path)?.is_none() {
        return Ok(None);
    }
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW);
    }
    let file = options.open(path).map_err(|_| fixed_error())?;
    let mut bytes = Vec::new();
    file.take((MAX_JSON_BYTES + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(|_| fixed_error())?;
    if bytes.len() > MAX_JSON_BYTES {
        return Err(fixed_error());
    }
    serde_json::from_slice(&bytes)
        .map(Some)
        .map_err(|_| fixed_error())
}

fn atomic_json<T: Serialize>(
    home: &Path,
    native: &Path,
    path: &Path,
    value: &T,
) -> Result<(), String> {
    let parent = path.parent().ok_or_else(fixed_error)?;
    private_dir(home, native, parent)?;
    validate_path(home, native, path, true)?;
    let bytes = serde_json::to_vec_pretty(value).map_err(|_| fixed_error())?;
    if bytes.len() > MAX_JSON_BYTES {
        return Err(fixed_error());
    }
    let temp = parent.join(format!(".registry-{}.tmp", Uuid::new_v4()));
    let result = (|| {
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600).custom_flags(libc::O_NOFOLLOW);
        }
        let mut file = options.open(&temp).map_err(|_| fixed_error())?;
        file.write_all(&bytes).map_err(|_| fixed_error())?;
        file.sync_all().map_err(|_| fixed_error())?;
        validate_path(home, native, path, true)?;
        fs::rename(&temp, path).map_err(|_| fixed_error())?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temp);
    }
    result
}

#[derive(Clone)]
pub struct Store {
    home: PathBuf,
    root: PathBuf,
}

impl Store {
    pub fn production() -> Result<Self, String> {
        let home = dirs::home_dir().ok_or_else(fixed_error)?;
        // resolve_data_dir is read-only, unlike get_data_dir; listing must not create it.
        let root = crate::modules::account::resolve_data_dir()?.join("claude_native");
        Ok(Self { home, root })
    }
    pub fn registry_path(&self) -> PathBuf {
        self.root.join("registry.json")
    }
    fn current_paths(&self) -> (PathBuf, PathBuf) {
        (
            self.home.join(".claude"),
            self.root.join("profiles").join(CURRENT_PROFILE_ID).join("desktop"),
        )
    }
    fn managed_paths(&self, id: &str) -> (PathBuf, PathBuf) {
        let root = self.root.join("profiles").join(id);
        (root.join("cli"), root.join("desktop"))
    }
    fn validate_registry(&self, registry: &Registry) -> Result<(), String> {
        if registry.version != 1 || registry.profiles.len() > MAX_PROFILES {
            return Err(fixed_error());
        }
        let mut ids = HashSet::new();
        let mut current_count = 0;
        for profile in &registry.profiles {
            validate_id(&profile.id)?;
            if !ids.insert(&profile.id)
                || !validate_label(&profile.label)
                || !validate_renewal(&profile.renewal_date)
                || profile.created_at < 0
                || profile.archived_at.map(|v| v <= 0).unwrap_or(false)
            {
                return Err(fixed_error());
            }
            let expected = match profile.kind {
                ProfileKind::Current => {
                    current_count += 1;
                    if profile.id != CURRENT_PROFILE_ID {
                        return Err(fixed_error());
                    }
                    self.current_paths()
                }
                ProfileKind::Managed => {
                    if profile.id == CURRENT_PROFILE_ID {
                        return Err(fixed_error());
                    }
                    self.managed_paths(&profile.id)
                }
            };
            if Path::new(&profile.config_dir) != expected.0
                || Path::new(&profile.desktop_dir) != expected.1
            {
                return Err(fixed_error());
            }
            validate_path(&self.home, &self.root, &expected.0, false)?;
            validate_path(&self.home, &self.root, &expected.1, false)?;
        }
        if current_count > 1
            || registry.profiles.iter().any(|p| p.archived && registry.selected_profile_id.as_deref() == Some(p.id.as_str()))
            || registry
                .selected_profile_id
                .as_ref()
                .map(|id| !ids.contains(id))
                .unwrap_or(false)
        {
            return Err(fixed_error());
        }
        Ok(())
    }
    fn read_registry(&self) -> Result<Registry, String> {
        let registry: Registry =
            read_private_json(&self.home, &self.root, &self.registry_path())?.unwrap_or_default();
        self.validate_registry(&registry)?;
        Ok(registry)
    }
    fn write_registry(&self, registry: &Registry) -> Result<(), String> {
        self.validate_registry(registry)?;
        atomic_json(&self.home, &self.root, &self.registry_path(), registry)
    }
    pub fn initialize(&self) -> Result<(), String> {
        let _guard = REGISTRY_LOCK.lock().map_err(|_| fixed_error())?;
        let registry = self.read_registry()?;
        private_dir(&self.home, &self.root, &self.root.join("snapshots"))?;
        if owned_metadata(&self.registry_path())?.is_none() {
            self.write_registry(&registry)?;
        }
        Ok(())
    }
    pub fn create(&self, label: String) -> Result<(), String> {
        if !validate_label(&label) {
            return Err("名称须为 1–80 个可显示字符。".into());
        }
        let _guard = REGISTRY_LOCK.lock().map_err(|_| fixed_error())?;
        let mut registry = self.read_registry()?;
        if registry.profiles.len() >= MAX_PROFILES {
            return Err("配置数量已达上限。".into());
        }
        let id = Uuid::new_v4().to_string();
        let (cli, desktop) = self.managed_paths(&id);
        private_dir(&self.home, &self.root, &cli)?;
        private_dir(&self.home, &self.root, &desktop)?;
        private_dir(&self.home, &self.root, &self.root.join("snapshots"))?;
        registry.profiles.push(ProfileMetadata {
            id: id.clone(),
            label: label.trim().into(),
            kind: ProfileKind::Managed,
            config_dir: cli.to_string_lossy().into(),
            desktop_dir: desktop.to_string_lossy().into(),
            created_at: now(),
            collector_installed: false,
            auto_refresh: true,
            renewal_date: None,
            archived: false,
            archived_at: None,
        });
        registry.selected_profile_id = Some(id);
        self.write_registry(&registry)
    }
    pub fn register_current(&self) -> Result<(), String> {
        let _guard = REGISTRY_LOCK.lock().map_err(|_| fixed_error())?;
        let mut registry = self.read_registry()?;
        if registry.profiles.iter().any(|p| p.id == CURRENT_PROFILE_ID) {
            return Ok(());
        }
        if registry.profiles.len() >= MAX_PROFILES {
            return Err("配置数量已达上限。".into());
        }
        let (cli, desktop) = self.current_paths();
        // The standard CLI is associated only by this explicit user action.
        // Do not create it implicitly or read/import any credential file.
        if !cli.is_dir() {
            return Err("未找到 ~/.claude；请先运行官方 Claude Code，或创建独立配置。".into());
        }
        validate_path(&self.home, &self.root, &cli, false)?;
        private_dir(&self.home, &self.root, &desktop)?;
        registry.profiles.push(ProfileMetadata {
            id: CURRENT_PROFILE_ID.into(),
            label: "标准 Claude Code 配置".into(),
            kind: ProfileKind::Current,
            config_dir: cli.to_string_lossy().into(),
            desktop_dir: desktop.to_string_lossy().into(),
            created_at: now(),
            collector_installed: false,
            auto_refresh: true,
            renewal_date: None,
            archived: false,
            archived_at: None,
        });
        if registry.selected_profile_id.is_none() {
            registry.selected_profile_id = Some(CURRENT_PROFILE_ID.into());
        }
        private_dir(&self.home, &self.root, &self.root.join("snapshots"))?;
        self.write_registry(&registry)
    }
    pub fn select(&self, id: &str) -> Result<(), String> {
        validate_id(id)?;
        let _guard = REGISTRY_LOCK.lock().map_err(|_| fixed_error())?;
        let mut registry = self.read_registry()?;
        if !registry.profiles.iter().any(|p| p.id == id && !p.archived) {
            return Err("没有找到此 Claude 配置。".into());
        }
        registry.selected_profile_id = Some(id.into());
        self.write_registry(&registry)
    }
    pub fn preferences(
        &self,
        id: &str,
        auto_refresh: bool,
        renewal_date: Option<String>,
    ) -> Result<(), String> {
        validate_id(id)?;
        if !validate_renewal(&renewal_date) {
            return Err("续费日期须为 YYYY-MM-DD 格式的有效日期。".into());
        }
        let _guard = REGISTRY_LOCK.lock().map_err(|_| fixed_error())?;
        let mut registry = self.read_registry()?;
        let profile = registry
            .profiles
            .iter_mut()
            .find(|p| p.id == id && !p.archived)
            .ok_or("没有找到此 Claude 配置。")?;
        profile.auto_refresh = auto_refresh;
        profile.renewal_date = renewal_date;
        self.write_registry(&registry)
    }
    fn mark_collector_installed(&self, id: &str) -> Result<(), String> {
        let _guard = REGISTRY_LOCK.lock().map_err(|_| fixed_error())?;
        let mut registry = self.read_registry()?;
        let profile = registry
            .profiles
            .iter_mut()
            .find(|p| p.id == id && !p.archived)
            .ok_or("没有找到此 Claude 配置。")?;
        profile.collector_installed = true;
        self.write_registry(&registry)
    }
    fn require_profile(&self, id: &str) -> Result<(), String> {
        validate_id(id)?;
        if !self.read_registry()?.profiles.iter().any(|p| p.id == id && !p.archived) {
            return Err("没有找到此 Claude 配置。".into());
        }
        Ok(())
    }
    fn require_known_profile(&self, id: &str) -> Result<(), String> {
        validate_id(id)?;
        if !self.read_registry()?.profiles.iter().any(|p| p.id == id) {
            return Err("没有找到此 Claude 配置。".into());
        }
        Ok(())
    }
    fn snapshot(&self, id: &str) -> (ClaudeAuth, ClaudeUsage) {
        let path = self.root.join("snapshots").join(format!("{}.json", id));
        match read_private_json::<Snapshot>(&self.home, &self.root, &path) {
            Ok(Some(snapshot)) => sanitize_snapshot(snapshot),
            Ok(None) => (ClaudeAuth::default(), ClaudeUsage::default()),
            Err(_) => (
                ClaudeAuth {
                    status: AuthStatus::Error,
                    ..ClaudeAuth::default()
                },
                ClaudeUsage {
                    error: Some(SNAPSHOT_ERROR.into()),
                    ..ClaudeUsage::default()
                },
            ),
        }
    }
    pub fn overview(&self) -> Result<ClaudeOverview, String> {
        let registry = self.read_registry()?;
        let archived_profiles = registry.profiles.iter().filter(|p| p.archived).map(|p| ArchivedClaudeProfile {
            id: p.id.clone(), label: p.label.clone(), kind: p.kind.clone(), archived_at: p.archived_at,
        }).collect();
        let profiles = registry
            .profiles
            .into_iter()
            .filter(|p| !p.archived)
            .map(|p| {
                let (auth, usage) = self.snapshot(&p.id);
                let health = read_private_json::<ClaudeHealth>(&self.home, &self.root,
                    &self.root.join("health").join(format!("{}.json", p.id)))
                    .ok().flatten().unwrap_or_default();
                ClaudeProfile {
                    selected: registry.selected_profile_id.as_deref() == Some(p.id.as_str()),
                    id: p.id,
                    label: p.label,
                    kind: p.kind,
                    config_dir: p.config_dir,
                    desktop_dir: p.desktop_dir,
                    created_at: p.created_at,
                    collector_installed: p.collector_installed,
                    auto_refresh: p.auto_refresh,
                    renewal_date: p.renewal_date,
                    auth,
                    usage,
                    health,
                }
            })
            .collect();
        let (desktop_running, code_process_count, warning) = process_summary();
        Ok(ClaudeOverview {
            profiles,
            archived_profiles,
            selected_profile_id: registry.selected_profile_id,
            desktop_running,
            code_process_count,
            checked_at: now(),
            warning,
        })
    }
}

fn sanitize_snapshot(mut snapshot: Snapshot) -> (ClaudeAuth, ClaudeUsage) {
    snapshot.auth.email = snapshot.auth.email.filter(|email| {
        email.len() <= 254
            && email.matches('@').count() == 1
            && email.split('@').all(|part| !part.is_empty())
            && !email.chars().any(|c| c.is_control() || c.is_whitespace())
    });
    snapshot.auth.plan = snapshot.auth.plan.filter(|plan| {
        matches!(
            plan.to_ascii_lowercase().as_str(),
            "free"
                | "pro"
                | "max"
                | "max_5x"
                | "max_20x"
                | "team"
                | "enterprise"
                | "claude_pro"
                | "claude_max"
        )
    });
    for window in [&mut snapshot.usage.five_hour, &mut snapshot.usage.seven_day] {
        if window
            .as_ref()
            .map(|v| !v.used_percent.is_finite() || !(0.0..=10_000.0).contains(&v.used_percent))
            .unwrap_or(false)
        {
            *window = None;
        }
        if let Some(value) = window {
            value.resets_at = value.resets_at.filter(|v| *v > 0);
        }
    }
    if let Some(extra) = &mut snapshot.usage.extra_usage {
        extra.used_minor = extra
            .used_minor
            .filter(|v| v.is_finite() && (0.0..=1e15).contains(v));
        extra.limit_minor = extra
            .limit_minor
            .filter(|v| v.is_finite() && (0.0..=1e15).contains(v));
        extra.currency = extra
            .currency
            .take()
            .filter(|c| c.len() == 3 && c.bytes().all(|v| v.is_ascii_uppercase()));
    }
    snapshot.auth.checked_at = snapshot.auth.checked_at.filter(|v| *v > 0);
    snapshot.usage.observed_at = snapshot.usage.observed_at.filter(|v| *v > 0);
    if snapshot.usage.error.is_some() {
        let code = snapshot.usage.error_code.as_deref().unwrap_or("unknown");
        let result = Action::Refresh.safe_result(false, Some(code));
        snapshot.usage.error_code = result.code;
        snapshot.usage.error = Some(result.message);
    } else {
        snapshot.usage.error_code = None;
    }
    (snapshot.auth, snapshot.usage)
}

fn process_summary() -> (bool, u32, Option<String>) {
    let mut command = Command::new("/bin/ps");
    command.args(["-axo", "comm="]).env_clear();
    match bounded_output(command, Duration::from_secs(5)) {
        Ok(bytes) => {
            let text = String::from_utf8_lossy(&bytes);
            let desktop = text
                .lines()
                .any(|p| p.trim().ends_with("/Claude.app/Contents/MacOS/Claude"));
            let count = text
                .lines()
                .filter(|p| {
                    p.trim().ends_with("/claude.app/Contents/MacOS/claude")
                        || p.contains("/.local/share/claude/versions/")
                        || p.trim().ends_with("/claude")
                        || p.trim() == "claude"
                })
                .count() as u32;
            (desktop, count, None)
        }
        Err(_) => (
            true,
            0,
            Some("无法确认 Claude 进程状态；Desktop 启动暂按忙碌处理。".into()),
        ),
    }
}

// /usr/bin/python3 may be an Apple developer-tool launcher rather than an
// installed interpreter. Never probe it by execution: that can open an installer.
fn usable_python(path: &Path) -> Option<PathBuf> {
    if path == Path::new("/usr/bin/python3") {
        return None;
    }
    let resolved = fs::canonicalize(path).ok()?;
    if resolved == Path::new("/usr/bin/python3") || !resolved.is_file() {
        return None;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if fs::metadata(&resolved).ok()?.permissions().mode() & 0o111 == 0 {
            return None;
        }
    }
    Some(resolved)
}

fn installed_python() -> Result<PathBuf, String> {
    for path in ["/opt/homebrew/bin/python3", "/usr/local/bin/python3"] {
        if let Some(python) = usable_python(Path::new(path)) {
            return Ok(python);
        }
    }
    // --print-path is read-only and does not invoke the tool-installation UI.
    let mut command = Command::new("/usr/bin/xcode-select");
    command.arg("--print-path").env_clear().env("PATH", "/usr/bin:/bin");
    if let Ok(bytes) = bounded_output(command, Duration::from_secs(3)) {
        if let Ok(text) = std::str::from_utf8(&bytes) {
            let developer = Path::new(text.trim());
            if developer.is_absolute() {
                if let Some(python) = usable_python(&developer.join("usr/bin/python3")) {
                    return Ok(python);
                }
            }
        }
    }
    usable_python(Path::new("/Library/Developer/CommandLineTools/usr/bin/python3"))
        .ok_or_else(|| PYTHON_MISSING.into())
}

#[derive(Clone)]
pub struct Runner {
    python: PathBuf,
    script: PathBuf,
}

impl Runner {
    pub fn for_app(app: &AppHandle) -> Result<Self, String> {
        let mut candidates = vec![];
        if let Ok(resource_dir) = app.path().resource_dir() {
            candidates.push(resource_dir.join("scripts").join(HELPER_NAME));
        }
        #[cfg(debug_assertions)]
        if let Some(root) = Path::new(env!("CARGO_MANIFEST_DIR")).parent() {
            candidates.push(root.join("scripts").join(HELPER_NAME));
        }
        let script = candidates
            .into_iter()
            .find(|p| {
                fs::symlink_metadata(p)
                    .map(|m| m.is_file() && !m.file_type().is_symlink())
                    .unwrap_or(false)
            })
            .ok_or("安装包缺少 Claude 本地助手；请重新构建预览版。")?;
        let python = installed_python()?;
        Ok(Self { python, script })
    }
    fn invoke<T: for<'de> Deserialize<'de>>(
        &self,
        store: &Store,
        action: Action,
        id: Option<&str>,
    ) -> Result<T, String> {
        self.invoke_script(
            store,
            &self.script,
            action.name(),
            id,
            &[],
            action.timeout(),
        )
    }
    fn invoke_script<T: for<'de> Deserialize<'de>>(
        &self,
        store: &Store,
        script: &Path,
        action: &str,
        id: Option<&str>,
        extra: &[&str],
        timeout: Duration,
    ) -> Result<T, String> {
        store.read_registry()?;
        if let Some(id) = id {
            store.require_profile(id)?;
        }
        let mut command = Command::new(&self.python);
        command
            .args(["-I", "-B"])
            .arg(script)
            .arg(action)
            .arg("--registry")
            .arg(store.registry_path());
        if let Some(id) = id {
            command.arg("--profile-id").arg(id);
        }
        command.args(extra);
        command
            .env_clear()
            .env("HOME", &store.home)
            .env(
                "PATH",
                std::env::var("PATH").unwrap_or_else(|_| "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin".into()),
            )
            .env("LANG", "en_US.UTF-8")
            .env("LC_ALL", "en_US.UTF-8");
        for key in [
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "NO_PROXY",
            "no_proxy",
        ] {
            if let Some(value) = std::env::var_os(key) {
                command.env(key, value);
            }
        }
        let bytes = bounded_output(command, timeout)?;
        serde_json::from_slice(&bytes).map_err(|_| HELPER_ERROR.to_string())
    }
    fn companion(&self) -> Result<PathBuf, String> {
        let path = self.script.with_file_name("claude-local-companion.py");
        match fs::symlink_metadata(&path) {
            Ok(metadata) if metadata.is_file() && !metadata.file_type().is_symlink() => Ok(path),
            _ => Err("安装包缺少 Claude 会话助手。".into()),
        }
    }
    pub fn cached_overview(&self, store: &Store) -> Result<ClaudeOverview, String> {
        // The companion projects every profile's local metadata and Desktop
        // cache without invoking Claude, writing snapshots or making requests.
        self.invoke_script(store, &self.companion()?, "overview", None, &[], Duration::from_secs(60))
    }
    pub fn archive_profile(&self, store: &Store, id: &str, archived: bool) -> Result<ClaudeActionResult, String> {
        let _registry_guard = REGISTRY_LOCK.lock().map_err(|_| fixed_error())?;
        store.require_known_profile(id)?;
        let script = self.script.with_file_name("claude-switch.py");
        if !fs::symlink_metadata(&script).map(|m| m.is_file() && !m.file_type().is_symlink()).unwrap_or(false) {
            return Err("安装包缺少可用的账号归档助手。".into());
        }
        let result: ClaudeActionResult = self.invoke_script(store, &script, if archived { "archive_profile" } else { "restore_profile" },
            None, &["--profile-id", id], Duration::from_secs(30))?;
        let message = if result.ok {
            if archived { "已从账号列表移除；凭据、会话、备份和切换回执均保留，可随时恢复。" }
            else { "已恢复到账号列表；没有切号、重新登录或恢复自动查询。" }
        } else { match result.code.as_deref() {
            Some("archive_selected") => "不能移除当前默认账号。",
            Some("archive_busy") => "该配置仍有所属进程或存在归属未知的进程，暂不能移除。",
            Some("archive_switch_pending") => "该配置仍被未完成的切换引用，请先结束该流程。",
            _ => "账号归档状态未改变；请确认本地记录与操作条件。",
        }};
        Ok(ClaudeActionResult { ok: result.ok, message: message.into(), code: result.code.filter(|v|
            matches!(v.as_str(), "archive_selected" | "archive_busy" | "archive_switch_pending")) })
    }
    pub fn switch_operation(&self, store: &Store, action: &str, source: Option<&str>, target: Option<&str>,
                            operation: Option<&str>, confirmed: bool) -> Result<serde_json::Value, String> {
        if !matches!(action, "list" | "prepare" | "advance" | "request_quit" | "confirm" | "verify_auto" | "rollback" | "cancel" | "ack_registration" | "retry_registration" | "retry_launch" | "abandon") {
            return Err("不支持此切换操作。".into());
        }
        let script = self.script.with_file_name("claude-switch.py");
        if !fs::symlink_metadata(&script).map(|m| m.is_file() && !m.file_type().is_symlink()).unwrap_or(false) {
            return Err("安装包缺少可用的切换助手。".into());
        }
        // Share the same writer mutex as create/select/preferences while the
        // child commits/restores the registry; no unrelated update can be lost.
        let _registry_guard = if matches!(action, "confirm" | "verify_auto" | "rollback" | "cancel") {
            Some(REGISTRY_LOCK.lock().map_err(|_| fixed_error())?)
        } else { None };
        let mut args = vec![];
        if let Some(value) = source { store.require_profile(value)?; args.extend(["--source-id", value]); }
        if let Some(value) = target { store.require_profile(value)?; args.extend(["--target-id", value]); }
        if let Some(value) = operation { validate_id(value)?; args.extend(["--operation-id", value]); }
        if confirmed { args.push("--confirmed"); }
        self.invoke_script(store, &script, action, None, &args, Duration::from_secs(360))
    }
    pub fn record_health(&self, store: &Store, id: &str, status: &str, reason: &str, source: &str, observed_at: i64) -> Result<ClaudeOverview, String> {
        store.require_profile(id)?;
        let time = observed_at.to_string();
        let result: ClaudeActionResult = self.invoke_script(store, &self.script, "record_health", Some(id),
            &["--health-status", status, "--health-reason", reason, "--health-source", source, "--observed-at", &time], Duration::from_secs(15))?;
        if !result.ok { return Err("官方证据未保存；请核对来源、状态和通知时间。".into()); }
        self.cached_overview(store)
    }
    pub fn inspect_auth(&self, store: &Store, id: &str) -> Result<ClaudeOverview, String> {
        let result: ClaudeActionResult = self.invoke(store, Action::InspectAuth, Some(id))?;
        let mut overview = store.overview()?;
        if !result.ok {
            overview.warning = Some("当前本地账号记录无法匹配；原有登录和会话未改动。".into());
        }
        Ok(overview)
    }
    pub fn sessions(&self, store: &Store, id: &str) -> Result<SessionInventory, String> {
        self.invoke_script(
            store,
            &self.companion()?,
            "sessions",
            Some(id),
            &[],
            Duration::from_secs(60),
        )
    }
    pub fn backup_sessions(&self, store: &Store, id: &str) -> Result<SessionBackup, String> {
        self.invoke_script(
            store,
            &self.companion()?,
            "snapshot",
            Some(id),
            &[],
            Duration::from_secs(300),
        )
    }
    pub fn resume_session(
        &self,
        store: &Store,
        source: &str,
        target: &str,
        session: &str,
    ) -> Result<ClaudeActionResult, String> {
        store.require_profile(target)?;
        validate_id(session)?;
        let result: ClaudeActionResult = self.invoke_script(
            store,
            &self.companion()?,
            "resume",
            Some(source),
            &["--target-profile-id", target, "--session-id", session],
            Duration::from_secs(180),
        )?;
        // The helper emits only fixed, user-safe messages; never forward raw errors.
        Ok(ClaudeActionResult {
            ok: result.ok,
            message: if result.ok { "已在官方 CLI 中请求恢复原会话；模型不会自动开始新一轮工作。" } else { "会话续接未完成。源记录与已有备份保留，目标可能已有写入；请核对回执、登录与网络条件后继续。" }.into(),
            code: result.code.filter(|v| matches!(v.as_str(), "busy" | "session_unavailable" | "network_unavailable" | "auth_required")),
        })
    }
    pub fn action(
        &self,
        store: &Store,
        id: &str,
        action: Action,
    ) -> Result<ClaudeActionResult, String> {
        let result: ClaudeActionResult = self.invoke(store, action, Some(id))?;
        if action == Action::InstallCollector && result.ok {
            store.mark_collector_installed(id)?;
        }
        Ok(action.safe_result(result.ok, result.code.as_deref()))
    }
    pub fn refresh(&self, store: &Store, id: &str) -> Result<ClaudeOverview, String> {
        let result = self.action(store, id, Action::Refresh);
        let mut overview = store.overview()?;
        if !matches!(result, Ok(ClaudeActionResult { ok: true, .. })) {
            overview.warning = Some(
                "有配置未更新：可能正在使用、尚未登录，或检查条件未满足；旧快照不代表当前用量。"
                    .into(),
            );
        }
        Ok(overview)
    }
    pub fn refresh_all(&self, store: &Store, automatic: bool) -> Result<ClaudeOverview, String> {
        let before = if automatic { self.cached_overview(store)? } else { store.overview()? };
        let mut incomplete = false;
        for profile in before
            .profiles
            .iter()
            .filter(|p| !automatic || (p.auto_refresh && p.auth.status == AuthStatus::SignedIn
                && !matches!(p.health.status.as_str(), "account_disabled" | "auth_required")
                && !matches!(p.usage.error_code.as_deref(), Some("account_disabled" | "auth_expired" | "auth_required" | "identity_mismatch"))))
        {
            if !matches!(
                self.action(store, &profile.id, Action::Refresh),
                Ok(ClaudeActionResult { ok: true, .. })
            ) {
                incomplete = true;
            }
            // Sequential and bounded. The frontend supplies the 30-minute schedule;
            // the helper enforces profile busy/lock checks and performs no model turn.
            // Explicit refresh also checks unknown/signed-out profiles so a freshly
            // completed official login can be discovered without enabling polling.
        }
        let mut overview = store.overview()?;
        if incomplete {
            overview.warning =
                Some("部分配置未刷新；正在使用的配置不会启动额外 CLI，旧快照已保留。".into());
        }
        Ok(overview)
    }
    pub fn preflight(&self, store: &Store) -> Result<ClaudePreflight, String> {
        store.initialize()?;
        let mut result: ClaudePreflight = self.invoke(store, Action::Preflight, None)?;
        if result.checks.len() > 20 {
            return Err(HELPER_ERROR.into());
        }
        result.cli_path = result.cli_path.filter(|p| {
            p.len() <= 4096 && Path::new(p).is_absolute() && !p.chars().any(char::is_control)
        });
        result.cli_version = result.cli_version.filter(|v| {
            v.len() <= 60 && v.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'.' || b == b'-')
        });
        // Accept only helper-owned labels; raw diagnostics never reach IPC.
        for check in result.checks.iter_mut() {
            let failure = match check.label.as_str() {
                "CLI 安装" => "未找到可验证的官方 CLI 安装；请安装 Claude Code 后重试。",
                "CLI 版本" => "无法读取 Claude Code 版本。",
                "客户端检查" => "客户端安装或权限检查未通过。",
                "本地配置" => "配置路径或权限检查未通过。",
                _ => {
                    check.label = "其他检查".into();
                    check.ok = false;
                    "此检查格式不受当前版本支持。"
                }
            };
            check.detail = if check.ok { "已通过" } else { failure }.into();
        }
        result.ok = result.ok && result.cli_path.is_some() && result.cli_version.is_some()
            && result.checks.iter().all(|c| c.ok)
            && ["CLI 安装", "CLI 版本"].iter()
                .all(|label| result.checks.iter().any(|check| check.label == *label));
        Ok(result)
    }
}

/// Bounded, nonblocking stdout. Stderr is deliberately discarded. On timeout only
/// the process group created for this invocation is terminated, never a named app.
#[cfg(unix)]
fn bounded_output(mut command: Command, timeout: Duration) -> Result<Vec<u8>, String> {
    use std::os::fd::AsRawFd;
    use std::os::unix::process::CommandExt;
    command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .process_group(0);
    let mut child = command.spawn().map_err(|_| HELPER_ERROR.to_string())?;
    let pid = child.id() as i32;
    let mut stdout = child
        .stdout
        .take()
        .ok_or_else(|| HELPER_ERROR.to_string())?;
    let fd = stdout.as_raw_fd();
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        unsafe {
            libc::kill(-pid, libc::SIGKILL);
        }
        let _ = child.wait();
        return Err(HELPER_ERROR.into());
    }
    let started = Instant::now();
    let mut output = Vec::new();
    let mut eof = false;
    loop {
        let mut buffer = [0u8; 8192];
        loop {
            match stdout.read(&mut buffer) {
                Ok(0) => {
                    eof = true;
                    break;
                }
                Ok(n) => {
                    output.extend_from_slice(&buffer[..n]);
                    if output.len() > MAX_JSON_BYTES {
                        break;
                    }
                }
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => break,
                Err(_) => {
                    unsafe {
                        libc::kill(-pid, libc::SIGKILL);
                    }
                    let _ = child.wait();
                    return Err(HELPER_ERROR.into());
                }
            }
        }
        if output.len() > MAX_JSON_BYTES || started.elapsed() > timeout {
            unsafe {
                libc::kill(-pid, libc::SIGTERM);
            }
            std::thread::sleep(Duration::from_millis(100));
            unsafe {
                libc::kill(-pid, libc::SIGKILL);
            }
            let _ = child.wait();
            return Err(HELPER_ERROR.into());
        }
        match child.try_wait() {
            Ok(Some(status)) if eof => {
                return if status.success() {
                    Ok(output)
                } else {
                    Err(HELPER_ERROR.into())
                }
            }
            Ok(_) => {}
            Err(_) => {
                unsafe {
                    libc::kill(-pid, libc::SIGKILL);
                }
                let _ = child.wait();
                return Err(HELPER_ERROR.into());
            }
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

#[cfg(not(unix))]
fn bounded_output(_command: Command, _timeout: Duration) -> Result<Vec<u8>, String> {
    Err("Claude 独立配置预览目前仅支持 macOS。".into())
}

#[cfg(test)]
mod tests {
    include!("claude_local_safe_tests.rs");
}
