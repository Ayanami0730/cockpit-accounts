import { useCallback, useEffect, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { Settings, X } from 'lucide-react';
import { useEscClose } from '../hooks/useEscClose';

/** Only explicit Cursor launch-path settings; no cross-platform configuration. */
export function LocalCursorSettings({
  requestKey = 0,
  hideTrigger = false,
}: { requestKey?: number; hideTrigger?: boolean } = {}) {
  const [visible, setVisible] = useState(false);
  const [path, setPath] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEscClose(visible && !busy, () => setVisible(false));

  const openSettings = useCallback(async () => {
    setVisible(true);
    setBusy(true);
    setError('');
    try {
      const config = await invoke<{ cursor_app_path?: string }>('get_general_config');
      setPath(config.cursor_app_path ?? '');
    } catch (cause) { setError(String(cause)); }
    finally { setBusy(false); }
  }, []);

  useEffect(() => {
    if (requestKey > 0) void openSettings();
  }, [requestKey, openSettings]);

  const detectPath = async () => {
    setBusy(true);
    setError('');
    try {
      const detected = await invoke<string | null>('detect_app_path', { app: 'cursor', force: true });
      if (detected) setPath(detected);
      else setError('没有找到 Cursor，请手动填写应用路径。');
    } catch (cause) { setError(String(cause)); }
    finally { setBusy(false); }
  };

  const savePath = async () => {
    setBusy(true);
    setError('');
    try {
      await invoke('patch_general_config', { updates: { cursor_app_path: path.trim() } });
      setVisible(false);
    } catch (cause) { setError(String(cause)); }
    finally { setBusy(false); }
  };

  return <>
    {!hideTrigger && <button className="btn btn-secondary icon-only" onClick={() => void openSettings()} title="Cursor 启动设置" aria-label="Cursor 启动设置"><Settings size={16} /></button>}
    {visible && <div className="modal-overlay">
      <section className="modal local-cursor-settings" role="dialog" aria-modal="true" aria-labelledby="local-cursor-settings-title">
        <div className="modal-header"><h2 id="local-cursor-settings-title">Cursor 启动设置</h2><button className="modal-close" onClick={() => setVisible(false)} disabled={busy} aria-label="关闭"><X /></button></div>
        <div className="modal-body">
          <label htmlFor="local-cursor-path">Cursor 应用路径</label>
          <input id="local-cursor-path" className="form-input" value={path} disabled={busy} onChange={(event) => setPath(event.target.value)} placeholder="/Applications/Cursor.app" />
          <p>仅用于你手动切换或启动 Cursor。自动刷新和后台任务已停用。</p>
          {error && <p role="alert" className="local-settings-error">{error}</p>}
        </div>
        <div className="modal-footer"><button className="btn btn-secondary" onClick={() => void detectPath()} disabled={busy}>检测路径</button><button className="btn btn-primary" onClick={() => void savePath()} disabled={busy}>保存</button></div>
      </section>
    </div>}
  </>;
}
