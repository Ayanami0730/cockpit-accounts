import { useState } from 'react';
import type { ClaudeHealth, ClaudeProfile } from '../types/localClaude';
import { healthLabel } from '../utils/localClaudeReminders';

const OPTIONS: Record<string, { label: string; status: ClaudeHealth['status']; source: ClaudeHealth['source'] }> = {
  usage_page_available: { label: '官方 Usage 页面正常返回', status: 'available', source: 'official_ui' },
  reauth_required: { label: '官方界面明确要求重新登录', status: 'auth_required', source: 'official_ui' },
  supported_countries_policy: { label: '官方邮件：因支持国家/地区政策停用', status: 'account_disabled', source: 'official_email' },
  official_notice: { label: '官方邮件明确通知账号停用', status: 'account_disabled', source: 'official_email' },
  officially_restored: { label: '已核对官方恢复通知，解除暂停并标为待复核', status: 'unknown', source: 'official_email' },
};
export function ClaudeHealthEvidence({ profile, now, disabled, onSave, onOpenOfficial }: {
  profile: ClaudeProfile; now: number; disabled: boolean;
  onSave: (status: ClaudeHealth['status'], reason: string, source: ClaudeHealth['source'], observedAt: number) => Promise<void>;
  onOpenOfficial: () => void;
}) {
  const [reason, setReason] = useState('');
  const [time, setTime] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const at = new Date(time).getTime();
  const health = profile.health;
  return <div className="lcl-health">
    <strong>{healthLabel(profile, now)}</strong>
    {health?.observedAt && <p>证据时间：{new Date(health.observedAt * 1000).toLocaleString()}；来源：{({ official_email: '你核对的官方邮件', official_ui: '你核对的官方界面', official_usage: '官方用量查询', official_cli: '官方 CLI', none: '无' })[health.source]}{health.reason === 'supported_countries_policy' ? '；支持国家/地区政策' : ''}</p>}
    {health?.lastAttemptOutcome === 'busy' && <p>上次检查因配置在用而跳过，没有重新验证可用性。</p>}
    {health?.status === 'account_disabled' && <p><button className="lcl-text-button" disabled={disabled} onClick={onOpenOfficial}>打开官方账号页</button>：请在网页核对账号后查看通知或申诉入口。此操作不提交申诉。</p>}
    <details><summary>记录已核对的官方证据</summary>
      <p>只保存状态、来源和时间，不粘贴邮件正文、账号标识或登录信息。缓存登录状态不会覆盖明确停用。</p>
      <label>观察结果<select value={reason} disabled={disabled} onChange={(event) => { setReason(event.target.value); setConfirmed(false); }}><option value="">请选择</option>{Object.entries(OPTIONS).map(([key, option]) => <option key={key} value={key}>{option.label}</option>)}</select></label>
      <label>通知或观察时间（本机时区）<input type="datetime-local" value={time} disabled={disabled} onChange={(event) => setTime(event.target.value)} /></label>
      <label className="lcl-checkbox"><input type="checkbox" checked={confirmed} disabled={disabled} onChange={(event) => setConfirmed(event.target.checked)} />我已核对这是该账号的官方证据，时间准确</label>
      <button className="btn btn-secondary" disabled={disabled || !OPTIONS[reason] || !confirmed || !Number.isFinite(at) || at > now + 60_000} onClick={() => { const option = OPTIONS[reason]; void onSave(option.status, reason, option.source, Math.floor(at / 1000)).then(() => { setReason(''); setTime(''); setConfirmed(false); }); }}>保存证据，不请求账号</button>
    </details>
  </div>;
}
