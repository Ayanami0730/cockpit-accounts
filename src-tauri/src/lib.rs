mod commands;
pub mod error;
mod models;
mod modules;
mod utils;

use modules::logger;
use std::sync::OnceLock;
#[cfg(target_os = "macos")]
use tauri::ActivationPolicy;
use tauri::Manager;
use tauri::RunEvent;
use tauri::WindowEvent;
use tracing::info;

/// 全局 AppHandle 存储
static APP_HANDLE: OnceLock<tauri::AppHandle> = OnceLock::new();

/// 获取全局 AppHandle
pub fn get_app_handle() -> Option<&'static tauri::AppHandle> {
    APP_HANDLE.get()
}

#[cfg(test)]
mod tests {
    use super::{
        has_enabled_periodic_account_refresh, should_hide_startup_minimized_window,
        should_preserve_main_window_for_background_refresh,
        should_preserve_main_window_for_menu_bar_refresh,
    };
    use crate::modules::config::UserConfig;

    #[test]
    fn local_safe_startup_has_no_automatic_ide_integration() {
        let source = include_str!("lib.rs");
        let runtime = source
            .rsplit("pub fn run() {")
            .next()
            .expect("runtime entry");
        let startup = runtime.split(".invoke_handler(").next().expect("startup");
        for forbidden in [
            "webkit_cache_maintenance::",
            "close_orphaned_legacy_platform_adapter_processes",
            "migrate_model_management_default_off_for_all_profiles",
            "sync_settings::",
            "websocket::start_server",
            "web_report::start_server",
            "codex_local_access::",
            "codex_session_history_sanitize::",
            "start_mixed_model_gateway_watchdog",
            "codex_app_injection::",
            "restore_pending_oauth_listener",
            "provider_token_keeper::",
            "auto_local_import::",
            "codex_temp_login::",
            "wakeup_scheduler::",
            "codex_wakeup_scheduler::",
            "external_import::",
            "create_tray_skeleton",
            "update_tray_menu",
            "workbuddy_auto_checkin::",
            "tauri_plugin_updater::",
            "tauri_plugin_autostart::",
        ] {
            assert!(
                !startup.contains(forbidden),
                "automatic integration: {forbidden}"
            );
        }
        assert!(!runtime.contains("restore_mixed_model_profiles_for_app_exit"));
        assert!(!runtime.contains("shutdown_local_access_gateway_for_app_exit"));
    }

    #[test]
    fn local_safe_account_services_cannot_bind_a_listener() {
        for source in [
            include_str!("modules/websocket.rs"),
            include_str!("modules/web_report.rs"),
        ] {
            assert!(!source.contains("TcpListener"));
            assert!(!source.contains("Listener::bind"));
        }
    }

    #[test]
    fn local_safe_settings_reject_other_ides_and_background_features() {
        for updates in [
            serde_json::json!({"codex_app_path": "/do-not-touch"}),
            serde_json::json!({"token_keeper_enabled": true}),
            serde_json::json!({"auto_import_from_local_enabled": true}),
            serde_json::json!({"cursor_auto_refresh_minutes": 10}),
            serde_json::json!({"language": "en"}),
        ] {
            let mut config = UserConfig::default();
            assert!(super::apply_local_cursor_preferences(
                &mut config,
                updates.as_object().expect("object")
            )
            .is_err());
        }
        assert!(super::require_cursor_app("cursor").is_ok());
        assert!(super::require_cursor_app("codex").is_err());
    }

    #[test]
    fn local_safe_settings_keep_manual_cursor_path_configuration() {
        let mut config = UserConfig::default();
        let updates = serde_json::json!({"cursor_app_path": " /Applications/Cursor.app "});
        super::apply_local_cursor_preferences(&mut config, updates.as_object().expect("object"))
            .expect("manual Cursor settings");
        assert_eq!(config.cursor_app_path, "/Applications/Cursor.app");
        assert_eq!(config.cursor_auto_refresh_minutes, -1);
    }

    #[test]
    fn startup_minimized_does_not_hide_when_disabled() {
        let mut config = UserConfig::default();
        config.startup_minimized = false;
        config.hide_dock_icon = true;

        assert!(!should_hide_startup_minimized_window(&config, true));
    }

    #[test]
    fn startup_minimized_hides_on_macos_when_dock_icon_is_hidden() {
        let mut config = UserConfig::default();
        config.startup_minimized = true;
        config.hide_dock_icon = true;

        assert!(should_hide_startup_minimized_window(&config, true));
    }

    #[test]
    fn startup_minimized_does_not_hide_when_dock_icon_is_available() {
        let mut config = UserConfig::default();
        config.startup_minimized = true;
        config.hide_dock_icon = false;

        assert!(!should_hide_startup_minimized_window(&config, true));
    }

    #[test]
    fn startup_minimized_does_not_wait_before_hiding_window() {
        let source = include_str!("lib.rs");
        let delayed_startup_hide = concat!(
            "std::thread::sleep",
            "(std::time::Duration::from_millis(300))"
        );

        assert!(!source.contains(delayed_startup_hide));
    }

    #[test]
    fn menu_bar_refresh_keeps_macos_main_webview_alive() {
        assert!(should_preserve_main_window_for_menu_bar_refresh(true, true));
        assert!(!should_preserve_main_window_for_menu_bar_refresh(
            true, false
        ));
    }

    #[test]
    fn menu_bar_refresh_does_not_change_non_macos_close_behavior() {
        assert!(!should_preserve_main_window_for_menu_bar_refresh(
            false, true
        ));
    }

    fn disable_all_periodic_refresh(config: &mut UserConfig) {
        config.auto_refresh_minutes = -1;
        config.codex_auto_refresh_minutes = -1;
        config.zed_auto_refresh_minutes = -1;
        config.ghcp_auto_refresh_minutes = -1;
        config.windsurf_auto_refresh_minutes = -1;
        config.kiro_auto_refresh_minutes = -1;
        config.cursor_auto_refresh_minutes = -1;
        config.grok_auto_refresh_minutes = -1;
        config.claude_auto_refresh_minutes = -1;
        config.codebuddy_auto_refresh_minutes = -1;
        config.codebuddy_cn_auto_refresh_minutes = -1;
        config.workbuddy_auto_refresh_minutes = -1;
        config.qoder_auto_refresh_minutes = -1;
        config.zcode_auto_refresh_minutes = -1;
        config.trae_auto_refresh_minutes = -1;
        config.trae_solo_auto_refresh_minutes = -1;
        config.trae_cn_auto_refresh_minutes = -1;
        config.trae_solo_cn_auto_refresh_minutes = -1;
    }

    #[test]
    fn windows_periodic_refresh_keeps_main_webview_alive() {
        let mut config = UserConfig::default();
        config.cursor_auto_refresh_minutes = 10;
        assert!(has_enabled_periodic_account_refresh(&config));
        assert!(should_preserve_main_window_for_background_refresh(
            false, true, false, &config
        ));
    }

    #[test]
    fn windows_without_periodic_refresh_keeps_destroy_behavior() {
        let mut config = UserConfig::default();
        disable_all_periodic_refresh(&mut config);
        assert!(!has_enabled_periodic_account_refresh(&config));
        assert!(!should_preserve_main_window_for_background_refresh(
            false, true, false, &config
        ));
    }

    #[test]
    fn periodic_refresh_does_not_change_non_windows_close_behavior() {
        let config = UserConfig::default();
        assert!(!should_preserve_main_window_for_background_refresh(
            false, false, false, &config
        ));
    }
}

#[cfg(any(target_os = "macos", target_os = "linux"))]
fn raise_process_file_descriptor_limit() {
    const TARGET_NOFILE_LIMIT: libc::rlim_t = 4096;

    unsafe {
        let mut limit = libc::rlimit {
            rlim_cur: 0,
            rlim_max: 0,
        };
        if libc::getrlimit(libc::RLIMIT_NOFILE, &mut limit) != 0 {
            logger::log_warn(&format!(
                "[Startup] 读取进程文件句柄上限失败: {}",
                std::io::Error::last_os_error()
            ));
            return;
        }

        let target = if limit.rlim_max == libc::RLIM_INFINITY {
            TARGET_NOFILE_LIMIT
        } else {
            TARGET_NOFILE_LIMIT.min(limit.rlim_max)
        };
        if target <= limit.rlim_cur || target == 0 {
            return;
        }

        let previous = limit.rlim_cur;
        limit.rlim_cur = target;
        if libc::setrlimit(libc::RLIMIT_NOFILE, &limit) == 0 {
            logger::log_info(&format!(
                "[Startup] 已提升进程文件句柄软限制: {} -> {}",
                previous, target
            ));
        } else {
            logger::log_warn(&format!(
                "[Startup] 提升进程文件句柄软限制失败: {} -> {}, error={}",
                previous,
                target,
                std::io::Error::last_os_error()
            ));
        }
    }
}

#[cfg(not(any(target_os = "macos", target_os = "linux")))]
fn raise_process_file_descriptor_limit() {}

fn should_hide_startup_minimized_window(
    config: &modules::config::UserConfig,
    is_macos: bool,
) -> bool {
    config.startup_minimized && is_macos && config.hide_dock_icon
}

fn should_preserve_main_window_for_menu_bar_refresh(
    is_macos: bool,
    menu_bar_quota_enabled: bool,
) -> bool {
    is_macos && menu_bar_quota_enabled
}

fn has_enabled_periodic_account_refresh(config: &modules::config::UserConfig) -> bool {
    [
        config.auto_refresh_minutes,
        config.codex_auto_refresh_minutes,
        config.zed_auto_refresh_minutes,
        config.ghcp_auto_refresh_minutes,
        config.windsurf_auto_refresh_minutes,
        config.kiro_auto_refresh_minutes,
        config.cursor_auto_refresh_minutes,
        config.grok_auto_refresh_minutes,
        config.claude_auto_refresh_minutes,
        config.codebuddy_auto_refresh_minutes,
        config.codebuddy_cn_auto_refresh_minutes,
        config.workbuddy_auto_refresh_minutes,
        config.qoder_auto_refresh_minutes,
        config.zcode_auto_refresh_minutes,
        config.trae_auto_refresh_minutes,
        config.trae_solo_auto_refresh_minutes,
        config.trae_cn_auto_refresh_minutes,
        config.trae_solo_cn_auto_refresh_minutes,
    ]
    .into_iter()
    .any(|minutes| minutes > 0)
}

fn should_preserve_main_window_for_background_refresh(
    is_macos: bool,
    is_windows: bool,
    menu_bar_quota_enabled: bool,
    config: &modules::config::UserConfig,
) -> bool {
    should_preserve_main_window_for_menu_bar_refresh(is_macos, menu_bar_quota_enabled)
        || (is_windows && has_enabled_periodic_account_refresh(config))
}

fn apply_startup_minimized(app: &tauri::AppHandle) {
    let config = modules::config::get_user_config();
    if !config.startup_minimized {
        return;
    }

    let should_hide = should_hide_startup_minimized_window(&config, cfg!(target_os = "macos"));
    let Some(window) = app.get_webview_window("main") else {
        logger::log_warn("[Window] 启动后自动最小化失败: main window not found");
        return;
    };

    let (result, action_label) = if should_hide {
        (window.hide(), "隐藏")
    } else {
        (window.minimize(), "最小化")
    };

    match result {
        Ok(()) => logger::log_info(&format!("[Window] 启动后已自动{}主窗口", action_label)),
        Err(err) => logger::log_warn(&format!("[Window] 启动后自动最小化失败: {}", err)),
    }
}

#[cfg(target_os = "macos")]
fn apply_macos_activation_policy(app: &tauri::AppHandle) {
    let config = modules::config::get_user_config();
    let (policy, dock_visible, policy_label) = if config.hide_dock_icon {
        (ActivationPolicy::Accessory, false, "hidden")
    } else {
        (ActivationPolicy::Regular, true, "visible")
    };

    if let Err(err) = app.set_activation_policy(policy) {
        logger::log_warn(&format!("[Window] 设置 macOS 激活策略失败: {}", err));
        return;
    }

    if let Err(err) = app.set_dock_visibility(dock_visible) {
        logger::log_warn(&format!("[Window] 设置 macOS Dock 可见性失败: {}", err));
    }

    if dock_visible {
        let _ = app.show();
        if let Some(window) = app.get_webview_window("main") {
            let _ = window.show();
        }
    }

    info!("[Window] 已应用 macOS Dock 图标策略: {}", policy_label);
}

// These command names match the shared Cursor UI, but deliberately avoid the
// upstream general-settings hooks (autostart, token keeper, sync, native menu).
#[tauri::command]
fn get_general_config() -> Result<serde_json::Value, String> {
    serde_json::to_value(modules::config::get_user_config()).map_err(|error| error.to_string())
}

fn apply_local_cursor_preferences(
    config: &mut modules::config::UserConfig,
    updates: &serde_json::Map<String, serde_json::Value>,
) -> Result<(), String> {
    for (key, value) in updates {
        match key.as_str() {
            "cursor_app_path" => {
                config.cursor_app_path = value
                    .as_str()
                    .ok_or("Cursor 路径必须是字符串")?
                    .trim()
                    .to_string();
            }
            "cursor_quota_alert_enabled" => {
                config.cursor_quota_alert_enabled =
                    value.as_bool().ok_or("额度提示必须是布尔值")?;
            }
            "cursor_quota_alert_threshold" => {
                let threshold = value.as_i64().ok_or("额度阈值必须是整数")?;
                if !(0..=100).contains(&threshold) {
                    return Err("额度阈值必须在 0 到 100 之间".to_string());
                }
                config.cursor_quota_alert_threshold = threshold as i32;
            }
            "cursor_auto_refresh_minutes" if value.as_i64() == Some(-1) => {
                config.cursor_auto_refresh_minutes = -1;
            }
            _ => return Err(format!("本地版不支持此设置: {key}")),
        }
    }
    Ok(())
}

#[tauri::command]
fn patch_general_config(updates: serde_json::Map<String, serde_json::Value>) -> Result<(), String> {
    modules::config::patch_user_config(|config| apply_local_cursor_preferences(config, &updates))?;
    Ok(())
}

fn require_cursor_app(app: &str) -> Result<(), String> {
    if app == "cursor" {
        Ok(())
    } else {
        Err("此本地版只管理 Cursor".to_string())
    }
}

#[tauri::command]
fn detect_app_path(app: String, force: Option<bool>) -> Result<Option<String>, String> {
    require_cursor_app(&app)?;
    commands::system::detect_app_path(app, force)
}

#[tauri::command]
async fn scan_app_launch_targets(
    app: String,
    scan_roots: Option<String>,
) -> Result<Vec<modules::process::AppLaunchCandidate>, String> {
    require_cursor_app(&app)?;
    commands::system::scan_app_launch_targets(app, scan_roots).await
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    logger::init_logger();
    modules::diagnostics::install_panic_hook();
    modules::diagnostics::start_frontend_ready_watchdog();
    raise_process_file_descriptor_limit();
    // 启动时先加载一次配置，确保进程级代理环境与用户设置同步。
    let _ = modules::config::get_user_config();

    #[cfg(target_os = "linux")]
    {
        if std::env::var_os("WEBKIT_DISABLE_DMABUF_RENDERER").is_none() {
            std::env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", "1");
            logger::log_info("[Linux] 设置 WEBKIT_DISABLE_DMABUF_RENDERER=1");
        }
    }

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_single_instance::init(|app, args, _cwd| {
            logger::log_info(&format!(
                "[SingleInstance] 收到唤起请求: arg_count={}",
                args.len()
            ));
            // Local Safe only raises its own window; arguments never import accounts.
            if let Err(err) = modules::floating_card_window::show_main_window(app) {
                logger::log_warn(&format!("[Window] 单实例唤起恢复主窗口失败: {}", err));
            }
        }))
        .setup(|app| {
            info!("Cockpit Tools 启动...");
            let current_exe = std::env::current_exe()
                .map(|path| path.display().to_string())
                .unwrap_or_else(|err| format!("unknown: {}", err));
            let build_mode = if cfg!(debug_assertions) {
                "debug"
            } else {
                "release"
            };
            logger::log_info(&format!(
                "[Startup] 启动诊断: marker=tray-diagnostics-v1, version={}, mode={}, exe={}",
                env!("CARGO_PKG_VERSION"),
                build_mode,
                current_exe
            ));

            // 存储全局 AppHandle
            let _ = APP_HANDLE.set(app.handle().clone());

            // Local Safe is intentionally passive on launch. Do not migrate or sanitize
            // IDE data, clean up other processes, synchronize extension settings, restore
            // gateways/injection/OAuth listeners, or start background account workers.
            // Manual Cursor commands remain available through the invoke handler.
            #[cfg(desktop)]
            {
                app.handle().plugin(tauri_plugin_process::init())?;
            }
            logger::log_info(
                "[LocalSafe] Automatic IDE integration and network services are disabled",
            );

            #[cfg(target_os = "macos")]
            apply_macos_activation_policy(&app.handle());

            // The upstream tray/native menu exposes other IDEs and reads their
            // identity files. This build uses only its own Cursor window.

            // Restore last main-window size/position before optional startup minimize (#948 / #1132).
            if let Some(main) = app.get_webview_window("main") {
                modules::main_window_state::restore_to_window(&main);
            }

            apply_startup_minimized(&app.handle());

            Ok(())
        })
        .on_window_event(|window, event| match event {
            WindowEvent::CloseRequested { .. } => {
                if window.label() == "main" {
                    modules::main_window_state::capture_and_save_from_window_handle(window);
                    modules::app_lifecycle::begin_shutdown();
                    window.app_handle().exit(0);
                }
            }
            WindowEvent::Resized(_) | WindowEvent::Moved(_) => {
                if window.label() == "main" {
                    modules::main_window_state::capture_and_save_from_window_handle_debounced(
                        window,
                    );
                }
            }
            _ => {}
        })
        .invoke_handler(tauri::generate_handler![
            commands::cursor::cursor_oauth_login_start,
            commands::cursor::cursor_oauth_login_complete,
            commands::cursor::cursor_oauth_login_cancel,
            commands::cursor::list_cursor_accounts,
            commands::cursor::delete_cursor_account,
            commands::cursor::delete_cursor_accounts,
            commands::cursor::import_cursor_from_json,
            commands::cursor::import_cursor_from_local,
            commands::cursor::export_cursor_accounts,
            commands::cursor::refresh_cursor_token,
            commands::cursor::refresh_all_cursor_tokens,
            commands::cursor::add_cursor_account_with_token,
            commands::cursor::update_cursor_account_tags,
            commands::cursor::get_cursor_accounts_index_path,
            commands::cursor::inject_cursor_account,
            commands::codex_local_safe::local_safe_codex_list,
            commands::codex_local_safe::local_safe_codex_import_local,
            commands::codex_local_safe::local_safe_codex_oauth_start,
            commands::codex_local_safe::local_safe_codex_oauth_complete,
            commands::codex_local_safe::local_safe_codex_oauth_cancel,
            commands::codex_local_safe::local_safe_codex_refresh_quota,
            commands::codex_local_safe::local_safe_codex_refresh_all,
            commands::codex_local_safe::local_safe_codex_reset_credits,
            commands::codex_local_safe::local_safe_codex_consume_reset_credit,
            commands::codex_local_safe::local_safe_codex_delete,
            commands::codex_local_safe::local_safe_codex_switch,
            commands::codex_local_safe::local_safe_codex_notify,
            commands::claude_local_safe::local_safe_claude_list,
            commands::claude_local_safe::local_safe_claude_archive_profile,
            commands::claude_local_safe::local_safe_claude_switch,
            commands::claude_local_safe::local_safe_claude_record_health,
            commands::claude_local_safe::local_safe_claude_inspect_auth,
            commands::claude_local_safe::local_safe_claude_sessions,
            commands::claude_local_safe::local_safe_claude_snapshot,
            commands::claude_local_safe::local_safe_claude_resume,
            commands::claude_local_safe::local_safe_claude_create,
            commands::claude_local_safe::local_safe_claude_register_current,
            commands::claude_local_safe::local_safe_claude_select,
            commands::claude_local_safe::local_safe_claude_refresh,
            commands::claude_local_safe::local_safe_claude_refresh_all,
            commands::claude_local_safe::local_safe_claude_preflight,
            commands::claude_local_safe::local_safe_claude_action,
            commands::claude_local_safe::local_safe_claude_preferences,
            commands::claude_local_safe::local_safe_claude_notify,
            commands::cursor_instance::cursor_get_instance_defaults,
            commands::cursor_instance::cursor_list_instances,
            commands::cursor_instance::cursor_create_instance,
            commands::cursor_instance::cursor_update_instance,
            commands::cursor_instance::cursor_delete_instance,
            commands::cursor_instance::cursor_start_instance,
            commands::cursor_instance::cursor_stop_instance,
            commands::cursor_instance::cursor_close_all_instances,
            commands::cursor_instance::cursor_open_instance_window,
            get_general_config,
            patch_general_config,
            scan_app_launch_targets,
            detect_app_path,
            commands::system::get_downloads_dir,
            commands::system::save_text_file,
            commands::system::open_data_folder,
            commands::system::diagnostics_capture_event,
            commands::system::diagnostics_frontend_stage,
            commands::system::diagnostics_frontend_ready,
            commands::system::load_user_memory,
            commands::system::mark_user_memory_dismissed,
            commands::system::save_user_memory_list,
            commands::system::load_ui_preferences,
            commands::system::save_ui_preferences,
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|app_handle, event| {
        match &event {
            RunEvent::ExitRequested { .. } | RunEvent::Exit => {
                // Only shut down this process; never rewrite another IDE's profile.
                modules::app_lifecycle::begin_shutdown();
            }
            _ => {}
        }

        #[cfg(target_os = "macos")]
        {
            match event {
                RunEvent::Reopen { .. } => {
                    if let Err(err) = modules::floating_card_window::show_main_window(app_handle) {
                        logger::log_warn(&format!("[Window] Dock 重新打开主窗口失败: {}", err));
                    }
                }
                _ => {}
            }
        }
        #[cfg(not(target_os = "macos"))]
        {
            let _ = (app_handle, event);
        }
    });
}
