import { useCallback, useEffect, useRef, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { listen, type UnlistenFn } from '@tauri-apps/api/event';
import { confirm as confirmDialog } from '@tauri-apps/plugin-dialog';
import { openUrl } from '@tauri-apps/plugin-opener';
import {
  collectReminders,
  creditBalance,
  daysUntil,
  parseSubscriptionExpiry,
  quotaWindows,
  SUBSCRIPTION_WARN_DAYS,
  weeklyWindow,
  type LocalCodexAccount,
} from '../utils/localCodexReminders';
import {
  creditsToUsd,
  formatCredits,
  formatUsdRange,
  getCodexPlanInfo,
  PRO_GRANT_EXPIRES_AT,
  remainingWeeklyUsd,
  USD_PER_CREDIT,
} from '../utils/localCodexPlans';

interface RemoteResult { host: string; ok: boolean; message: string; runningAppServers?: number | null }
interface SwitchResult {
  account: LocalCodexAccount;
  codexWasRunning: boolean;
  relaunched: boolean;
  otherCodexProcesses: number;
  remote?: RemoteResult | null;
  notes: string[];
}

interface ResetCredit { id?: string; status?: string; expires_at?: number | null; granted_at?: number | null }
interface ResetCreditsSnapshot { available_count?: number | null; credits: ResetCredit[]; next_expires_at?: number | null }
interface OAuthStart { loginId: string; authUrl: string }
type Message = { kind: 'info' | 'error'; text: string };

const SHOWN_REMINDERS_KEY = 'local-safe.codex.shown-reminders';
const RELAUNCH_KEY = 'local-safe.codex.relaunch';
const AUTO_REFRESH_MS = 30 * 60 * 1000;

function readFlag(key: string, fallback: boolean): boolean {
  try {
    const value = localStorage.getItem(key);
    return value === null ? fallback : value === '1';
  } catch {
    return fallback;
  }
}

function writeFlag(key: string, value: boolean) {
  try { localStorage.setItem(key, value ? '1' : '0'); } catch { /* storage unavailable */ }
}

function takeUnshownReminderKeys(keys: string[]): Set<string> {
  let shown: string[] = [];
  try { shown = JSON.parse(localStorage.getItem(SHOWN_REMINDERS_KEY) ?? '[]'); } catch { shown = []; }
  const fresh = new Set(keys.filter((key) => !shown.includes(key)));
  if (fresh.size > 0) {
    try {
      localStorage.setItem(SHOWN_REMINDERS_KEY, JSON.stringify([...shown, ...fresh].slice(-300)));
    } catch { /* storage unavailable */ }
  }
  return fresh;
}

function formatTime(seconds?: number | null): string {
  if (!seconds) return '未知';
  return new Date(seconds * 1000).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function formatRelative(seconds?: number | null): string {
  if (!seconds) return '';
  const diff = seconds * 1000 - Date.now();
  if (diff <= 0) return '已到重置时间';
  const hours = Math.floor(diff / 3_600_000);
  if (hours >= 24) return `${Math.floor(hours / 24)} 天 ${hours % 24} 小时后`;
  if (hours >= 1) return `${hours} 小时 ${Math.floor((diff % 3_600_000) / 60_000)} 分后`;
  return `${Math.max(1, Math.floor(diff / 60_000))} 分钟后`;
}

function errorText(error: unknown): string {
  return typeof error === 'string' ? error : error instanceof Error ? error.message : JSON.stringify(error);
}

function CreditsFact({ account, planKey }: { account: LocalCodexAccount; planKey: string }) {
  const credits = account.usageCredits;
  if (!credits) return <span className="lc-muted">积分：接口未返回（刷新一次再看）</span>;
  if (credits.unlimited) return <span>积分：<strong>不限</strong></span>;
  const balance = creditBalance(account);
  if (balance === null) return <span>积分：<strong>{credits.hasCredits ? '有，余额未知' : '0'}</strong></span>;
  const grantDays = planKey === 'pro' && balance > 0 && Date.now() < PRO_GRANT_EXPIRES_AT
    ? daysUntil(PRO_GRANT_EXPIRES_AT, Date.now())
    : null;
  return (
    <span title={`按官方充值价 1,000 积分 = $40（$${USD_PER_CREDIT}/积分）折算。套餐额度先用，用完才扣积分。`}>
      积分：<strong>{formatCredits(balance)}</strong>（约 ${creditsToUsd(balance).toLocaleString('en-US')}）
      {grantDays !== null && (
        <span className={grantDays <= 14 ? 'lc-warn-text' : 'lc-muted'}>，补偿积分 12/31 过期，还剩 {grantDays} 天</span>
      )}
    </span>
  );
}

function QuotaBar({ label, percent, resetTime, present }: { label: string; percent: number; resetTime?: number | null; present?: boolean | null }) {
  if (present === false) return null;
  const level = percent <= 10 ? 'low' : percent <= 30 ? 'mid' : 'ok';
  return (
    <div className="lc-quota">
      <div className="lc-quota-head">
        <span>{label}剩余 <strong>{percent}%</strong></span>
        <span className="lc-muted">重置 {formatTime(resetTime)}（{formatRelative(resetTime)}）</span>
      </div>
      <div className="lc-bar"><div className={`lc-bar-fill lc-${level}`} style={{ width: `${Math.max(0, Math.min(100, percent))}%` }} /></div>
    </div>
  );
}

export function LocalCodexPage() {
  const [accounts, setAccounts] = useState<LocalCodexAccount[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<Message | null>(null);
  const [oauth, setOauth] = useState<OAuthStart | null>(null);
  const [relaunch, setRelaunch] = useState(() => readFlag(RELAUNCH_KEY, true));
  const [credits, setCredits] = useState<Record<string, ResetCreditsSnapshot>>({});
  const previous = useRef(new Map<string, LocalCodexAccount>());
  const oauthRef = useRef<OAuthStart | null>(null);
  oauthRef.current = oauth;

  const applyAccounts = useCallback((next: LocalCodexAccount[]) => {
    const reminders = collectReminders(next, previous.current, Date.now());
    const fresh = takeUnshownReminderKeys(reminders.map((reminder) => reminder.key));
    for (const reminder of reminders) {
      if (fresh.has(reminder.key)) {
        void invoke('local_safe_codex_notify', { title: reminder.title, body: reminder.body }).catch(() => {});
      }
    }
    previous.current = new Map(next.map((account) => [account.id, account]));
    setAccounts(next);
  }, []);

  const run = useCallback(async <T,>(label: string, task: () => Promise<T>): Promise<T | undefined> => {
    setBusy(label);
    try {
      return await task();
    } catch (error) {
      setMessage({ kind: 'error', text: errorText(error) });
      return undefined;
    } finally {
      setBusy(null);
    }
  }, []);

  const loadList = useCallback(async () => {
    const list = await invoke<LocalCodexAccount[]>('local_safe_codex_list');
    applyAccounts(list);
    return list;
  }, [applyAccounts]);

  const refreshAll = useCallback(async () => {
    const list = await invoke<LocalCodexAccount[]>('local_safe_codex_refresh_all');
    applyAccounts(list);
  }, [applyAccounts]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const list = await loadList();
        if (!cancelled && list.length > 0) await refreshAll();
      } catch (error) {
        if (!cancelled) setMessage({ kind: 'error', text: errorText(error) });
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    const timer = window.setInterval(() => { void refreshAll().catch(() => {}); }, AUTO_REFRESH_MS);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [loadList, refreshAll]);

  useEffect(() => {
    let disposed = false;
    let unlisten: UnlistenFn | undefined;
    void listen<{ loginId?: string; login_id?: string }>('codex-oauth-login-completed', (event) => {
      const loginId = event.payload?.loginId ?? event.payload?.login_id;
      const pending = oauthRef.current;
      if (!pending || loginId !== pending.loginId) return;
      void run('登录', async () => {
        const account = await invoke<LocalCodexAccount>('local_safe_codex_oauth_complete', { loginId });
        setOauth(null);
        await loadList();
        setMessage({ kind: 'info', text: `已添加 ${account.email}` });
      });
    }).then((stop) => { if (disposed) stop(); else unlisten = stop; }).catch(() => {});
    return () => { disposed = true; unlisten?.(); };
  }, [loadList, run]);

  const startOAuth = () => run('登录', async () => {
    const start = await invoke<OAuthStart>('local_safe_codex_oauth_start');
    setOauth(start);
    setMessage({ kind: 'info', text: '请在浏览器里登录要添加的账号。浏览器已经登着别的号时，把链接复制到无痕窗口里打开。' });
  });

  const cancelOAuth = () => run('取消登录', async () => {
    const pending = oauth;
    await invoke('local_safe_codex_oauth_cancel', { loginId: pending?.loginId ?? null });
    setOauth(null);
    setMessage({ kind: 'info', text: '已取消本次登录，未添加或切换账号。' });
  });

  const importLocal = () => run('导入', async () => {
    const account = await invoke<LocalCodexAccount>('local_safe_codex_import_local');
    await loadList();
    setMessage({ kind: 'info', text: `已导入本机 Codex 当前账号 ${account.email}` });
  });

  const refreshOne = (id: string) => run(`刷新:${id}`, async () => {
    await invoke('local_safe_codex_refresh_quota', { accountId: id });
    await loadList();
  });

  const loadCredits = (id: string) => run(`重置卡:${id}`, async () => {
    const snapshot = await invoke<ResetCreditsSnapshot>('local_safe_codex_reset_credits', { accountId: id });
    setCredits((value) => ({ ...value, [id]: snapshot }));
  });

  const consumeCredit = async (account: LocalCodexAccount) => {
    const ok = await confirmDialog(`确定给 ${account.email} 用掉一张重置卡吗？用掉后不能撤回。`, { title: '使用重置卡', kind: 'warning' });
    if (!ok) return;
    await run(`重置卡:${account.id}`, async () => {
      await invoke('local_safe_codex_consume_reset_credit', { accountId: account.id });
      await loadList();
      setCredits((value) => { const next = { ...value }; delete next[account.id]; return next; });
      setMessage({ kind: 'info', text: `已为 ${account.email} 使用一张重置卡` });
    });
  };

  const switchTo = async (account: LocalCodexAccount) => {
    const ok = await confirmDialog(
      `切换到 ${account.email}？\n\n会先退出 Codex（有任务在跑时 Codex 会拦住，切换随即取消），写入新账号${relaunch ? '，然后重新打开 Codex' : ''}。`,
      { title: '切换 Codex 账号', kind: 'warning' },
    );
    if (!ok) return;
    await run(`切换:${account.id}`, async () => {
      const result = await invoke<SwitchResult>('local_safe_codex_switch', { accountId: account.id, relaunch });
      await loadList();
      const parts = [`已切换到 ${result.account.email}。`];
      if (result.remote) parts.push(`${result.remote.host}：${result.remote.message}`);
      parts.push(...result.notes);
      setMessage({ kind: result.remote && !result.remote.ok ? 'error' : 'info', text: parts.join(' ') });
    });
  };

  const removeAccount = async (account: LocalCodexAccount) => {
    const ok = await confirmDialog(`从登号器里删除 ${account.email}？不会退出或吊销这个账号。`, { title: '删除账号', kind: 'warning' });
    if (!ok) return;
    await run(`删除:${account.id}`, async () => {
      await invoke('local_safe_codex_delete', { accountId: account.id });
      await loadList();
    });
  };


  const sorted = [...accounts].sort((a, b) => Number(b.isCurrent) - Number(a.isCurrent)
    || (weeklyWindow(b.quota)?.percent ?? -1) - (weeklyWindow(a.quota)?.percent ?? -1));

  return (
    <section className="lc-page">
      <div className="lc-toolbar">
        <div className="lc-actions">
          <button className="btn btn-primary" disabled={!!busy} onClick={() => void startOAuth()}>登录新账号</button>
          <button className="btn btn-secondary" disabled={!!busy} onClick={() => void importLocal()}>导入本机当前账号</button>
          <button className="btn btn-secondary" disabled={!!busy} onClick={() => void run('全部刷新', refreshAll)}>全部刷新</button>
        </div>
        <div className="lc-options">
          <label><input type="checkbox" checked={relaunch} onChange={(event) => { setRelaunch(event.target.checked); writeFlag(RELAUNCH_KEY, event.target.checked); }} /> 换号后重新打开 Codex</label>
        </div>
      </div>

      {oauth && (
        <div className="lc-panel">
          <p>在浏览器完成登录后，账号会自动加进来。</p>
          <code className="lc-url">{oauth.authUrl}</code>
          <div className="lc-actions">
            <button className="btn btn-primary" onClick={() => void openUrl(oauth.authUrl)}>用默认浏览器打开</button>
            <button className="btn btn-secondary" onClick={() => { void navigator.clipboard.writeText(oauth.authUrl); setMessage({ kind: 'info', text: '链接已复制，可以粘到无痕窗口里。' }); }}>复制链接</button>
            <button className="btn btn-secondary" disabled={!!busy} onClick={cancelOAuth}>取消</button>
          </div>
        </div>
      )}


      {message && <div className={`lc-panel ${message.kind === 'error' ? 'lc-error' : ''}`} role="status">{message.text}</div>}
      {busy && <div className="lc-muted lc-busy">正在处理：{busy.split(':')[0]}…</div>}

      {loading ? <div className="loading-state">正在读取 Codex 账号…</div> : sorted.length === 0 ? (
        <div className="lc-panel">还没有账号。先点「导入本机当前账号」把正在用的号存进来，其他号用「登录新账号」添加。不要在 Codex 里点退出登录来换号，那样会把旧号的登录吊销掉。</div>
      ) : (
        <div className="lc-grid">
          {sorted.map((account) => {
            const expiry = parseSubscriptionExpiry(account.subscriptionActiveUntil);
            const days = expiry === null ? null : daysUntil(expiry, Date.now());
            const quota = account.quota;
            const plan = getCodexPlanInfo(account.planType);
            const weeklyLeftUsd = remainingWeeklyUsd(plan, weeklyWindow(quota)?.percent);
            const snapshot = credits[account.id];
            const available = snapshot?.available_count ?? quota?.reset_credits_available;
            const busyHere = busy?.endsWith(`:${account.id}`) ?? false;
            return (
              <article key={account.id} className={`lc-card ${account.isCurrent ? 'lc-current' : ''}`}>
                <header className="lc-card-head">
                  <div>
                    <div className="lc-email">{account.email}</div>
                    <div className="lc-badges">
                      <span className={`lc-badge lc-plan-${plan.key}`} title={account.planType ?? ''}>{plan.label}{plan.monthlyUsd ? ` · $${plan.monthlyUsd}/月` : ''}</span>
                      {account.isLiveLocal && <span className="lc-badge lc-badge-live">本机在用</span>}
                      {account.isCurrent && !account.isLiveLocal && <span className="lc-badge">上次切换</span>}
                      {account.requiresReauth && <span className="lc-badge lc-badge-warn">需重新登录</span>}
                    </div>
                  </div>
                </header>
                {quota ? (
                  quotaWindows(quota).map((window) => (
                    <QuotaBar key={window.label} label={window.label} percent={window.percent} resetTime={window.resetTime} />
                  ))
                ) : <div className="lc-muted">还没有额度数据，点刷新。</div>}
                <div className="lc-facts">
                  <span title={`${plan.note}。社区估算：以 Plus 用满一周约 $55–135 的 API 价折算为基准，乘以官方倍数；OpenAI 未公布周上限，实际随模型和缓存变化很大。`}>
                    周额度折合：{plan.weeklyUsd ? <><strong>{formatUsdRange(plan.weeklyUsd)}</strong>/周{weeklyLeftUsd ? `，本周约剩 ${formatUsdRange(weeklyLeftUsd)}` : ''}</> : '无固定额度估算'}
                    <span className="lc-muted">（社区估算，非官方）</span>
                  </span>
                  <CreditsFact account={account} planKey={plan.key} />
                  <span>重置卡：<strong>{available ?? '未知'}</strong>{quota?.reset_credits_next_expires_at ? `（最近一张 ${formatTime(quota.reset_credits_next_expires_at)} 过期）` : ''}</span>
                  <span className={days !== null && days <= SUBSCRIPTION_WARN_DAYS ? 'lc-warn-text' : days !== null && days <= 7 ? 'lc-mid-text' : ''}>
                    订阅：{expiry === null ? '未知' : `${new Date(expiry).toLocaleDateString('zh-CN')} 到期（${days! <= 0 ? '已到期' : `还有 ${days} 天`}）`}
                  </span>
                  <span className="lc-muted">额度更新于 {formatTime(account.usageUpdatedAt)}</span>
                </div>
                {snapshot && snapshot.credits.length > 0 && (
                  <ul className="lc-credits">
                    {snapshot.credits.map((credit, index) => (
                      <li key={credit.id ?? index}>{credit.status ?? '未知状态'} · 到期 {formatTime(credit.expires_at)}</li>
                    ))}
                  </ul>
                )}
                {account.quotaError && <div className="lc-error-text">{account.quotaError}</div>}
                <div className="lc-actions">
                  {!account.isLiveLocal && <button className="btn btn-primary" disabled={!!busy} onClick={() => void switchTo(account)}>切换到这个号</button>}
                  <button className="btn btn-secondary" disabled={!!busy} onClick={() => void refreshOne(account.id)}>{busyHere && busy?.startsWith('刷新') ? '刷新中…' : '刷新'}</button>
                  <button className="btn btn-secondary" disabled={!!busy} onClick={() => void loadCredits(account.id)}>重置卡明细</button>
                  {(available ?? 0) > 0 && <button className="btn btn-secondary" disabled={!!busy} onClick={() => void consumeCredit(account)}>用一张重置卡</button>}
                  {!account.isLiveLocal && <button className="btn btn-secondary lc-danger" disabled={!!busy} onClick={() => void removeAccount(account)}>删除</button>}
                </div>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
