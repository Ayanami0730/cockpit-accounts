import { Suspense, lazy, useEffect, useState } from 'react';
import { getVersion } from '@tauri-apps/api/app';
import { getCurrentWindow } from '@tauri-apps/api/window';
import { invoke } from '@tauri-apps/api/core';
import { listen, type UnlistenFn } from '@tauri-apps/api/event';
import { BootReadyMarker } from './components/BootReadyMarker';
import { GlobalModal } from './components/GlobalModal';
import { LocalCursorSettings } from './components/LocalCursorSettings';
import './App.css';
import { LocalCodexPage } from './pages/LocalCodexPage';
import { LocalClaudePage } from './pages/LocalClaudePage';
import './LocalApp.css';

const CursorAccountsPage = lazy(() =>
  import('./pages/CursorAccountsPage').then((module) => ({ default: module.CursorAccountsPage })),
);

// This entry deliberately does not mount the upstream App or SettingsPage.
// Those components initialize cross-IDE services, takeover, scheduled jobs,
// remote configuration, and the upstream updater when mounted.
export default function LocalApp() {
  const [page, setPage] = useState<'codex' | 'cursor' | 'claude' | 'settings'>('codex');
  const [codexVisited, setCodexVisited] = useState(false);
  const [version, setVersion] = useState('0.1.0');
  const [pathSettingsRequest, setPathSettingsRequest] = useState(0);

  useEffect(() => { if (page === 'codex') setCodexVisited(true); }, [page]);

  useEffect(() => {
    document.title = 'Cockpit Accounts';
    void getVersion().then(setVersion).catch(() => {});
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    const syncTheme = () => {
      document.documentElement.setAttribute('data-theme', media.matches ? 'dark' : 'light');
    };
    syncTheme();
    media.addEventListener('change', syncTheme);
    return () => media.removeEventListener('change', syncTheme);
  }, []);

  useEffect(() => {
    let disposed = false;
    let unlisten: UnlistenFn | undefined;
    const showPathSettings = (payload?: { app?: string }) => {
      if (payload?.app === 'cursor') setPathSettingsRequest((value) => value + 1);
    };
    const onMissingPath = (event: Event) => {
      showPathSettings((event as CustomEvent<{ app?: string }>).detail);
    };
    window.addEventListener('app-path-missing', onMissingPath);
    void listen<{ app?: string }>('app:path_missing', (event) => {
      showPathSettings(event.payload);
    }).then((stop) => {
      if (disposed) stop();
      else unlisten = stop;
    }).catch(() => {});
    return () => {
      disposed = true;
      unlisten?.();
      window.removeEventListener('app-path-missing', onMissingPath);
    };
  }, []);

  useEffect(() => {
    const navigate = (event: Event) => {
      const target = (event as CustomEvent<string>).detail;
      setPage(target === 'cursor' || target === 'codex' || target === 'claude' ? target : 'settings');
    };
    window.addEventListener('app-request-navigate', navigate);
    return () => window.removeEventListener('app-request-navigate', navigate);
  }, []);

  return (
    <div className="local-app">
      <div className="local-titlebar" data-tauri-drag-region onMouseDown={(event) => {
        if (event.button === 0) void getCurrentWindow().startDragging().catch(() => {});
      }}>Cockpit Accounts</div>
      <header className="local-header">
        <div className="local-brand"><strong>Cockpit Accounts</strong><span>macOS · {version}</span></div>
        <nav className="local-nav" aria-label="主导航">
          <button className={`btn ${page === 'codex' ? 'btn-primary' : 'btn-secondary'}`} onClick={() => setPage('codex')} aria-current={page === 'codex' ? 'page' : undefined}>Codex 账号</button>
          <button className={`btn ${page === 'cursor' ? 'btn-primary' : 'btn-secondary'}`} onClick={() => setPage('cursor')} aria-current={page === 'cursor' ? 'page' : undefined}>Cursor 账号</button>
          <button className={`btn ${page === 'claude' ? 'btn-primary' : 'btn-secondary'}`} onClick={() => setPage('claude')} aria-current={page === 'claude' ? 'page' : undefined}>Claude 账号</button>
          <button className={`btn ${page === 'settings' ? 'btn-primary' : 'btn-secondary'}`} onClick={() => setPage('settings')} aria-current={page === 'settings' ? 'page' : undefined}>设置</button>
        </nav>
      </header>
      <div className="local-build-notice" role="note">
        已停用上游更新、自动历史清理和后台服务；账户凭据不写入浏览器缓存。
        切换 Cursor 或 Codex 账号都会先关闭对应的应用，请先保存工作、等任务跑完。
        Claude 使用独立目录；选择账号只影响下次启动，运行中的会话保持原样。
      </div>
      <main className="local-content">
        <div hidden={page !== 'codex'}>
          {codexVisited && <LocalCodexPage />}
        </div>
        <div hidden={page !== 'claude'}>
          <LocalClaudePage />
        </div>
        {page === 'codex' || page === 'claude' ? null : page === 'cursor' ? (
          <Suspense fallback={<div className="loading-state">正在加载 Cursor 账号…</div>}>
            <CursorAccountsPage />
            <BootReadyMarker />
          </Suspense>
        ) : (
          <section className="local-settings">
            <h1>设置</h1>
            <p>此版本使用独立的管理器数据目录，提供 Cursor、Codex 和 Claude 账号管理。启动时不自动导入本机账号，不恢复上游接管、网关或会话清理。</p>
            <h2>Claude 操作</h2>
            <p>每个账号使用独立配置目录，由未修改的官方客户端完成登录和保存凭据。管理器只保存标签与用量摘要，不读取或转移 OAuth、Cookie 和设备标识。用量查询经官方 CLI；有工作进程时不切换 Desktop，后台刷新不会发送模型提示。</p>
            <p>重置次数和未取得的积分余额显示未知，使用或购买额度在官方页面完成。实验性 CLI 查询不可用时保留最近一次观察及时间，不把旧数据当作实时结果。</p>
            <h2>Codex 操作</h2>
            <p>切换 Codex 账号只改写其配置的认证存储（<code>auth.json</code> 或钥匙串），不改 <code>config.toml</code>、会话记录或 Codex 应用本身。切换前先把当前账号最新的凭据存回登号器，再请 Codex 正常退出；Codex 拦住退出时切换取消，不会强制结束进程。</p>
            <p>Codex 页面打开期间每 30 分钟刷新一次额度，订阅三天内到期、当前账号周额度低于 10%、以及周额度重置时发系统通知。登号器不替正在使用的账号刷新凭据。</p>
            <h2>更新已停用</h2>
            <p>为保留本地修复，此安装包不检查、下载或安装上游更新。需要升级时，应先审核并重新合入这些修复，再安装新的本地构建。</p>
            <h2>账户与凭据</h2>
            <p>账户数据由本地管理器保存，前端只在内存中展示。旧版浏览器账户缓存会在启动时移除；这不会删除管理器中的账户。导出文件仍可能包含凭据，请自行保管。</p>
            <h2>Cursor 操作</h2>
            <p>导入本机账号和切换账号只在你点击对应按钮后执行。切换会修改 Cursor 的登录状态，并关闭、重新启动 Cursor；请先保存正在编辑的内容。</p>
            <p>配额查询、OAuth 登录和手动刷新仍会联系 Cursor 服务。此版本未启用自动刷新、自动签到、自动备份、唤醒任务或代理服务。</p>
            <button className="btn btn-secondary" onClick={() => { void invoke('open_data_folder').catch(() => {}); }}>打开管理器数据目录</button>
            <BootReadyMarker />
          </section>
        )}
      </main>
      <BootReadyMarker />
      <GlobalModal />
      <LocalCursorSettings requestKey={pathSettingsRequest} hideTrigger />
    </div>
  );
}
