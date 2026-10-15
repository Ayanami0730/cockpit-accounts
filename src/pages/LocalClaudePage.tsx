import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { AlertCircle, Check, ChevronDown, Clock3, ExternalLink, Archive, FolderOpen, Loader2, Monitor, Plus, RefreshCw, ShieldCheck, Terminal } from 'lucide-react';
import { createLocalClaudeService } from '../services/localClaudeService';
import type { ClaudeAction, ClaudeOverview, ClaudePreflight, ClaudeProfile, ClaudeUsage, WindowUsage, ClaudeSession, ClaudeSessionInventory, ClaudeSessionSnapshot } from '../types/localClaude';
import { CLAUDE_AUTO_REFRESH_MS, canAutoRefreshClaude, collectorStatus, collectClaudeReminders, formatMinorAmount, refreshRetryDelay, renewalDays, takeUnseenClaudeReminders, usageFreshness, usedPercent, windowExpired, sessionResumeBlockReason, formatSessionBytes, sessionActivityLabel } from '../utils/localClaudeReminders';
import './LocalClaudePage.css';
import { LocalClaudeSwitch } from '../components/LocalClaudeSwitch';
import { ClaudeHealthEvidence } from '../components/ClaudeHealthEvidence';

type Notice = { kind: 'info' | 'error'; text: string };
type RetryAction = 'load' | 'refresh' | 'preflight';
const REMINDER_KEYS = 'local-safe.claude.reminder-keys';
const EMPTY: ClaudeOverview = { profiles: [], selectedProfileId: null, desktopRunning: false, codeProcessCount: 0, checkedAt: 0, warning: null };
const AUTH_LABEL = { unknown: '身份待检查', signed_in: '上次 CLI 已登录', signed_out: '上次 CLI 未登录', error: '身份记录读取失败' };

function dateTime(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds) || seconds <= 0) return '未知';
  return new Date(seconds * 1000).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}
function resetLabel(seconds: number | null, now: number): string {
  if (seconds === null || !Number.isFinite(seconds) || seconds <= 0) return '重置时间未知';
  if (seconds * 1000 <= now) return '窗口已过，待刷新';
  const minutes = Math.max(1, Math.ceil((seconds * 1000 - now) / 60000));
  const remaining = minutes >= 1440 ? `${Math.floor(minutes / 1440)} 天 ${Math.floor(minutes % 1440 / 60)} 小时` : minutes >= 60 ? `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分` : `${minutes} 分钟`;
  return `${remaining}后重置`;
}
function sourceLabel(usage: ClaudeUsage): string {
  return usage.source === 'official_cli' ? '官方 CLI · 实验查询' : usage.source === 'official_statusline' ? '官方状态栏' : usage.source === 'desktop_cache' ? 'Desktop 缓存' : '尚无用量来源';
}

function Quota({ label, window, usage, now }: { label: string; window: WindowUsage | null; usage: ClaudeUsage; now: number }) {
  const percent = usedPercent(window?.usedPercent);
  const expired = windowExpired(window, now);
  const stale = usageFreshness(usage, now) !== 'fresh' || expired;
  const tone = percent !== null && percent >= 100 ? 'limit' : percent !== null && percent >= 90 ? 'near' : 'normal';
  return <div className={`lcl-quota ${stale ? 'lcl-quota-stale' : ''}`}>
    <div className="lcl-quota-heading"><span>{label}</span><strong>{percent === null ? '尚未同步' : <>{percent.toLocaleString('zh-CN', { maximumFractionDigits: 1 })}<small>% 已用</small></>}</strong></div>
    <div className={`lcl-meter lcl-meter-${tone} ${percent === null ? 'lcl-meter-unknown' : ''}`} role={percent === null ? undefined : 'progressbar'} aria-label={`${label}已用`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent ?? undefined} aria-valuetext={percent === null ? '未知' : `${percent}%${stale ? '，上次采集值' : ''}`}>
      {percent !== null && <span style={{ width: `${percent}%` }} />}
    </div>
    <div className="lcl-quota-reset" title={dateTime(window?.resetsAt ?? null)}><Clock3 size={12} aria-hidden="true" />{resetLabel(window?.resetsAt ?? null, now)}{expired && percent !== null ? '，保留上次值' : ''}</div>
  </div>;
}

function ExtraUsage({ usage }: { usage: ClaudeUsage }) {
  const extra = usage.extraUsage;
  if (!extra) return <><dt>额外用量</dt><dd>尚未读取</dd></>;
  if (!extra.enabled) return <><dt>额外用量</dt><dd>未启用</dd></>;
  const used = formatMinorAmount(extra.usedMinor, extra.currency);
  const limit = formatMinorAmount(extra.limitMinor, extra.currency);
  return <><dt>额外用量</dt><dd>已用 {used ?? '未知'}<span className="lcl-detail-secondary">上限 {limit ?? '未知'}</span></dd></>;
}

function ProfilePreferences({ profile, disabled, onSave }: { profile: ClaudeProfile; disabled: boolean; onSave: (autoRefresh: boolean, renewalDate: string | null) => Promise<void> }) {
  const [renewalDate, setRenewalDate] = useState(profile.renewalDate ?? '');
  useEffect(() => { setRenewalDate(profile.renewalDate ?? ''); }, [profile.renewalDate]);
  const valid = renewalDate === '' || renewalDays(renewalDate, Date.now()) !== null;
  return <div className="lcl-preferences">
    <label className="lcl-checkbox"><input type="checkbox" checked={profile.autoRefresh} disabled={disabled} onChange={(event) => { void onSave(event.target.checked, profile.renewalDate); }} />管理器运行时每 30 分钟尝试刷新；在用时跳过</label>
    <form className="lcl-renewal" onSubmit={(event) => { event.preventDefault(); if (valid) void onSave(profile.autoRefresh, renewalDate || null); }}>
      <label htmlFor={`renewal-${profile.id}`}>续费提醒日期<span>选填，以你的记录为准</span></label>
      <div><input id={`renewal-${profile.id}`} type="date" value={renewalDate} onChange={(event) => setRenewalDate(event.target.value)} disabled={disabled} aria-invalid={!valid} /><button className="btn btn-secondary" type="submit" disabled={disabled || !valid || renewalDate === (profile.renewalDate ?? '')}>保存</button></div>
    </form>
    {!valid && <p className="lcl-inline-error">请输入有效日期。</p>}
  </div>;
}

export function LocalClaudePage({ readonly = false }: { readonly?: boolean }) {
  const service = useMemo(() => createLocalClaudeService(readonly), [readonly]);
  const [overview, setOverview] = useState<ClaudeOverview>(EMPTY);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [newLabel, setNewLabel] = useState('');
  const [adding, setAdding] = useState(false);
  const [preflight, setPreflight] = useState<ClaudePreflight | null>(null);
  const [pendingLogins, setPendingLogins] = useState<Map<string, number>>(new Map());
  const [now, setNow] = useState(Date.now());
  const [retryAt, setRetryAt] = useState(0);
  const [retryAction, setRetryAction] = useState<RetryAction | null>(null);
  const [autoDeferred, setAutoDeferred] = useState(false);
  const [sessionSourceId, setSessionSourceId] = useState('');
  const [sessionInventory, setSessionInventory] = useState<ClaudeSessionInventory | null>(null);
  const [targetInventory, setTargetInventory] = useState<ClaudeSessionInventory | null>(null);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [sessionsError, setSessionsError] = useState<string | null>(null);
  const [snapshots, setSnapshots] = useState<Record<string, ClaudeSessionSnapshot>>({});
  const mounted = useRef(false);
  const busyRef = useRef(false);
  const overviewRef = useRef(overview);
  const shown = useRef(new Set<string>());
  const failures = useRef(0);
  const sessionRequest = useRef(0);
  const passiveReading = useRef(false);
  const nextAutoAt = useRef(Date.now() + CLAUDE_AUTO_REFRESH_MS);
  const switchBusy = useCallback((active: boolean) => { busyRef.current = active; setBusy(active ? '切换账号' : null); }, []);

  const applyOverview = useCallback((next: ClaudeOverview, notify = false) => {
    if (!mounted.current) return;
    overviewRef.current = next;
    setOverview(next);
    setPendingLogins((pending) => new Map([...pending].filter(([id, started]) => {
      const auth = next.profiles.find((profile) => profile.id === id)?.auth;
      return auth?.status !== 'signed_in' || auth.checkedAt === null || auth.checkedAt < started;
    })));
    if (!notify || readonly) return;
    const fresh = takeUnseenClaudeReminders(collectClaudeReminders(next.profiles, Date.now()), shown.current);
    if (fresh.length) {
      try { localStorage.setItem(REMINDER_KEYS, JSON.stringify([...shown.current])); } catch { /* In-memory dedup remains active. */ }
      for (const reminder of fresh) void service.notify(reminder.title, reminder.body).catch(() => { /* No notification retry storm. */ });
    }
  }, [readonly, service]);

  const run = useCallback(async (label: string, task: () => Promise<void>, retry: RetryAction | null = null) => {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(label); setNotice(null);
    try {
      await task();
      if (mounted.current) { failures.current = 0; setRetryAction(null); setRetryAt(0); setAutoDeferred(false); }
    } catch {
      if (mounted.current) {
        failures.current += 1;
        const after = Date.now() + refreshRetryDelay(failures.current);
        setRetryAt(after); setRetryAction(retry);
        nextAutoAt.current = Date.now() + CLAUDE_AUTO_REFRESH_MS;
        setNotice({ kind: 'error', text: `${label}未完成。现有会话未被切换，请稍后重试。` });
      }
    } finally { busyRef.current = false; if (mounted.current) setBusy(null); }
  }, []);

  const loadSessions = useCallback(async (sourceId: string, targetId: string | null) => {
    if (!sourceId) return;
    const request = ++sessionRequest.current;
    setSessionsLoading(true); setSessionsError(null);
    try {
      const source = await service.sessions(sourceId);
      const target = targetId ? (targetId === sourceId ? source : await service.sessions(targetId)) : null;
      if (mounted.current && request === sessionRequest.current) { setSessionInventory(source); setTargetInventory(target); }
    } catch {
      if (mounted.current && request === sessionRequest.current) {
        setSessionsError('暂时无法读取本地会话。原记录没有被更改。'); setSessionInventory(null); setTargetInventory(null);
      }
    } finally { if (mounted.current && request === sessionRequest.current) setSessionsLoading(false); }
  }, [service]);

  useEffect(() => {
    setSessionSourceId((current) => overview.profiles.some((profile) => profile.id === current) ? current : (overview.profiles.find((profile) => profile.kind === 'current')?.id ?? overview.profiles[0]?.id ?? ''));
  }, [overview.profiles]);

  useEffect(() => {
    setSessionInventory(null); setTargetInventory(null);
    if (sessionSourceId) void loadSessions(sessionSourceId, overview.selectedProfileId);
    return () => { sessionRequest.current += 1; };
  }, [loadSessions, sessionSourceId, overview.selectedProfileId]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (busyRef.current || passiveReading.current) return;
      passiveReading.current = true;
      // Both native and browser list project every registered profile locally,
      // including a managed profile in use. Never start a CLI for this timer.
      void service.list().then((next) => {
        if (!mounted.current) return;
        applyOverview(next);
        return loadSessions(sessionSourceId, next.selectedProfileId);
      }).catch(() => { if (mounted.current) setSessionsError('本地摘要更新未完成，保留已显示的记录。'); })
        .finally(() => { passiveReading.current = false; });
    }, 30_000);
    return () => window.clearInterval(timer);
  }, [applyOverview, loadSessions, readonly, service, sessionSourceId]);

  useEffect(() => {
    mounted.current = true;
    let disposed = false;
    setLoading(true); setOverview(EMPTY); overviewRef.current = EMPTY;
    shown.current = new Set();
    if (!readonly) {
      try {
        const saved: unknown = JSON.parse(localStorage.getItem(REMINDER_KEYS) ?? '[]');
        if (Array.isArray(saved)) shown.current = new Set(saved.filter((key): key is string => typeof key === 'string' && /^(quota|renewal):/.test(key) && key.length < 200).slice(-300));
      } catch { /* Do not persist account data as a fallback. */ }
    }
    void service.list().then((next) => { if (!disposed) applyOverview(next); }).catch(() => {
      if (!disposed) { setNotice({ kind: 'error', text: '暂时无法读取 Claude 账号。请确认本地服务可用后重试。' }); setRetryAction('load'); }
    }).finally(() => { if (!disposed) setLoading(false); });
    const clock = window.setInterval(() => setNow(Date.now()), 1000);
    return () => { disposed = true; mounted.current = false; window.clearInterval(clock); };
  }, [applyOverview, readonly, service]);

  useEffect(() => {
    if (readonly) return;
    const timer = window.setInterval(() => {
      if (busyRef.current || passiveReading.current || loading || Date.now() < nextAutoAt.current) return;
      const profiles = overviewRef.current.profiles.filter(canAutoRefreshClaude);
      nextAutoAt.current = Date.now() + CLAUDE_AUTO_REFRESH_MS;
      if (!profiles.length) return;
      busyRef.current = true; setBusy('后台刷新');
      void (async () => {
        try {
          applyOverview(await service.refreshAll(true), true);
          if (mounted.current) { setAutoDeferred(false); failures.current = 0; }
        } catch {
          // Keep the old data visible and wait for the next interval; never retry authentication.
          if (mounted.current) setAutoDeferred(true);
        } finally { busyRef.current = false; if (mounted.current) setBusy(null); }
      })();
    }, 30_000);
    return () => window.clearInterval(timer);
  }, [applyOverview, loading, readonly, service]);

  const refreshAll = () => run(readonly ? '刷新本地摘要' : '刷新用量', async () => {
    const next = readonly ? await service.list() : await service.refreshAll(false);
    applyOverview(next, !readonly);
    await loadSessions(sessionSourceId, next.selectedProfileId);
    setNotice({ kind: 'info', text: readonly ? '已读取本地安全摘要，没有查询账号服务或更改会话。' : '刷新检查完成。在用配置会跳过查询；未返回的新数据保持未知。' });
  }, 'refresh');
  const checkClient = () => run('检查客户端', async () => {
    const result = await service.preflight(); setPreflight(result);
    if (!result.ok) setNotice({ kind: 'error', text: '客户端检查未通过。请按下方提示安装或配置客户端后重试。' });
  }, 'preflight');
  const retry = () => {
    if (retryAction === 'load') void run('读取账号', async () => applyOverview(await service.list()), 'load');
    else if (retryAction === 'preflight') void checkClient();
    else void refreshAll();
  };
  const createProfile = (event: FormEvent) => {
    event.preventDefault(); const label = newLabel.trim(); if (!label) return;
    void run('创建账号配置', async () => {
      applyOverview(await service.create(label)); setNewLabel(''); setAdding(false);
      setNotice({ kind: 'info', text: '配置已创建。接下来在官方 CLI 或 Desktop 中分别登录。' });
    });
  };
  const act = (profile: ClaudeProfile, action: ClaudeAction) => {
    const labels: Record<ClaudeAction, string> = { login_cli: '打开官方登录', launch_cli: '启动 CLI', launch_desktop: '启动 Desktop', open_usage: '打开官方用量页', install_collector: '启用用量采集' };
    void run(labels[action], async () => {
      const result = await service.action(profile.id, action);
      setNotice({ kind: result.ok ? 'info' : 'error', text: result.message });
      if (result.ok && action === 'login_cli') setPendingLogins((value) => new Map(value).set(profile.id, Math.floor(Date.now() / 1000)));
      if (result.ok && (action === 'install_collector' || action === 'launch_desktop')) applyOverview(await service.list());
    });
  };
  const inspectAuth = (profile: ClaudeProfile) => run('检查当前身份', async () => {
    applyOverview(await service.inspectAuth(profile.id));
    setNotice({ kind: 'info', text: '已读取本地账号记录，没有发起登录或服务端身份验证。' });
  });
  const preserveSessions = () => run('备份全部会话', async () => {
    const result = await service.snapshot(sessionSourceId);
    setSnapshots((previous) => ({ ...previous, [sessionSourceId]: result }));
    setNotice({ kind: result.ok ? 'info' : 'error', text: result.ok ? '原始会话已保全。仍在写入或不完整的文件已单独标记，没有删除原记录。' : '会话备份未完成，请查看保全结果；原记录没有被删除。' });
    await loadSessions(sessionSourceId, overview.selectedProfileId);
  });
  const resumeSession = (session: ClaudeSession) => run('按保存记录继续会话', async () => {
    const targetId = overview.selectedProfileId;
    if (!targetId || sessionResumeBlockReason(sessionInventory, targetInventory, session)) return;
    const result = await service.resume(sessionSourceId, targetId, session.sessionId);
    setNotice({ kind: result.ok ? 'info' : 'error', text: result.message });
    await loadSessions(sessionSourceId, targetId);
  });
  const archiveProfile = (profileId: string, archived: boolean) => run(archived ? '从列表移除账号' : '恢复账号到列表', async () => {
    const result = await service.archiveProfile(profileId, archived);
    setNotice({ kind: result.ok ? 'info' : 'error', text: result.message });
    if (result.ok) applyOverview(await service.list());
  });
  const desktopBusy = overview.desktopRunning || overview.codeProcessCount > 0;
  const disabled = loading || busy !== null;
  const writeDisabled = disabled || readonly;
  const sorted = [...overview.profiles].sort((a, b) => Number(b.id === overview.selectedProfileId) - Number(a.id === overview.selectedProfileId));
  const backup = snapshots[sessionSourceId] ?? sessionInventory?.lastBackup;
  const collected = overview.profiles.filter((profile) => usageFreshness(profile.usage, now) === 'fresh').length;

  return <section className="lcl-page" aria-label="Claude 账号管理">
    <header className="lcl-header">
      <div className="lcl-heading"><h1>Claude 账号</h1></div>
      <div className="lcl-actions">
        {!readonly && <button className="btn btn-secondary" disabled={disabled} onClick={() => void checkClient()}><ShieldCheck size={15} />检查客户端</button>}
        <button className="btn btn-secondary" disabled={disabled || (!readonly && !overview.profiles.length) || now < retryAt} onClick={() => void refreshAll()}><RefreshCw size={15} className={busy === '刷新用量' ? 'lcl-spin' : ''} />{readonly ? '刷新本地摘要' : '刷新用量'}</button>
        {!readonly && <button className="btn btn-primary" disabled={disabled || !overview.selectedProfileId || overview.profiles.find((profile) => profile.id === overview.selectedProfileId)?.health?.status === 'account_disabled'} title="使用下次默认账号，从本工具启动一个新的 CLI，不切换正在运行的会话" onClick={() => { const selected = overview.profiles.find((profile) => profile.id === overview.selectedProfileId); if (selected) act(selected, 'launch_cli'); }}><Terminal size={15} />启动默认 CLI</button>}
        {!readonly && <button className="btn btn-secondary" disabled={disabled} onClick={() => setAdding((value) => !value)}><Plus size={15} />添加账号</button>}
      </div>
    </header>

    {readonly && <div className="lcl-readonly-banner"><ShieldCheck size={16} /><span>已关联配置的只读视图。启动、登录和保全会话请在 Cockpit 应用中操作。</span></div>}
    <div className="lcl-status-strip">
      <span><span className={`lcl-status-dot ${desktopBusy ? 'is-busy' : ''}`} />{desktopBusy ? `Desktop / Code 正在运行${overview.codeProcessCount ? `（${overview.codeProcessCount} 个 Code 进程）` : ''}` : overview.checkedAt > 0 ? 'Desktop 当前未运行' : '客户端状态尚未读取'}</span>
      <span>{loading ? '读取中' : `${overview.profiles.length} 个账号配置，${collected} 个有近期数据`}</span>
      {busy && <span role="status"><Loader2 size={13} className="lcl-spin" />{busy}…</span>}
    </div>
    {desktopBusy && <p className="lcl-quiet-note">Desktop 启动暂不可用，避免打断现有会话。选择默认账号只影响下次从本工具启动。</p>}
    {overview.warning && <div className="lcl-notice lcl-notice-info"><AlertCircle size={16} /><span>{overview.warning}</span></div>}
    {autoDeferred && <div className="lcl-notice lcl-notice-info"><Clock3 size={16} /><span>后台刷新未完成，保留上次数据。30 分钟后再检查，也可手动重试。</span></div>}
    {notice && <div className={`lcl-notice lcl-notice-${notice.kind}`} role={notice.kind === 'error' ? 'alert' : 'status'}>
      {notice.kind === 'error' ? <AlertCircle size={17} /> : <Check size={17} />}<span>{notice.text}</span>
      {notice.kind === 'error' && retryAction && <button className="lcl-text-button" disabled={disabled || now < retryAt} onClick={retry}>{now < retryAt ? `${Math.ceil((retryAt - now) / 1000)} 秒后重试` : '重试'}</button>}
      <button className="lcl-dismiss" aria-label="关闭提示" onClick={() => setNotice(null)}>×</button>
    </div>}

    {adding && !readonly && <form className="lcl-add-form" onSubmit={createProfile}>
      <label htmlFor="claude-profile-label">账号名称<input id="claude-profile-label" autoFocus maxLength={60} value={newLabel} onChange={(event) => setNewLabel(event.target.value)} placeholder="例如：个人开发、工作账号" disabled={disabled} /></label>
      <button className="btn btn-primary" disabled={disabled || !newLabel.trim()} type="submit">创建配置</button>
      <button className="btn btn-secondary" type="button" disabled={disabled} onClick={() => setAdding(false)}>取消</button>
      <p>这里只创建独立配置，下一步通过官方客户端登录。</p>
    </form>}

    {preflight && <details className="lcl-preflight" open>
      <summary><ShieldCheck size={16} />客户端{preflight.ok ? '检查通过' : '需要处理'}<span>{dateTime(preflight.checkedAt)}</span><ChevronDown size={15} /></summary>
      <ul>{preflight.checks.map((check, index) => <li key={`${check.label}-${index}`} className={check.ok ? '' : 'lcl-check-failed'}>{check.ok ? <Check size={15} /> : <AlertCircle size={15} />}<div><strong>{check.label}</strong><span>{check.detail}</span></div></li>)}</ul>
      <p>CLI {preflight.cliVersion ?? '版本未知'} · {preflight.cliPath ?? '尚未找到可执行文件'}</p>
    </details>}

    {loading ? <div className="lcl-empty" role="status"><Loader2 className="lcl-spin" size={22} /><p>正在读取本地账号配置…</p></div> : sorted.length === 0 ? <div className="lcl-empty">
      <Terminal size={28} /><h2>添加第一个配置</h2><p>关联本机标准 CLI 配置 <code>~/.claude</code>，或创建独立配置后登录。</p>
      {!readonly && <div className="lcl-actions"><button className="btn btn-primary" disabled={writeDisabled} onClick={() => void run('关联现用配置', async () => { applyOverview(await service.registerCurrent()); setNotice({ kind: 'info', text: '现用配置已关联。没有复制凭据或切换登录。' }); })}>关联现用配置</button><button className="btn btn-secondary" disabled={writeDisabled} onClick={() => setAdding(true)}>创建独立配置</button></div>}
    </div> : <div className="lcl-grid">{sorted.map((profile) => {
      const selected = profile.id === overview.selectedProfileId;
      const freshness = usageFreshness(profile.usage, now);
      const anyExpired = windowExpired(profile.usage.fiveHour, now) || windowExpired(profile.usage.sevenDay, now);
      const pending = pendingLogins.has(profile.id);
      const suspended = profile.health?.status === 'account_disabled';
      return <article key={profile.id} className={`lcl-account ${selected ? 'is-selected' : ''}`} aria-label={profile.label}>
        <header className="lcl-account-header"><div><h2>{profile.label}</h2><p>{profile.auth.email ?? (profile.kind === 'current' ? '现有身份待检查' : '登录后显示账号信息')}</p></div>{selected && <span className="lcl-default-badge"><Check size={12} />下次默认</span>}</header>
        <div className="lcl-badges">
          {profile.auth.plan && <span className="lcl-badge">{profile.auth.plan}</span>}
          <span className={`lcl-badge ${profile.auth.status === 'signed_in' ? 'lcl-auth-ok' : 'lcl-auth-pending'}`}>{pending ? '等待官方登录确认' : profile.auth.source === 'cached_metadata' ? '本地账号记录' : AUTH_LABEL[profile.auth.status]}</span>
          {profile.kind === 'current' && <span className="lcl-badge">原有配置</span>}
        </div>
        <p className="lcl-current-note">{profile.auth.source === 'official_cli' ? `官方 CLI 登录记录：${dateTime(profile.auth.checkedAt)}。不代表当前服务健康或 Desktop 登录状态。` : profile.auth.source === 'cached_metadata' ? '身份来自本地记录，尚无官方 CLI 登录检查结果。' : '尚无可用身份记录。'}</p>
        <ClaudeHealthEvidence profile={profile} now={now} disabled={writeDisabled} onOpenOfficial={() => act(profile, 'open_usage')} onSave={async (status, reason, source, observedAt) => {
          await run('保存官方证据', async () => applyOverview(await service.recordHealth(profile.id, status, reason, source, observedAt)));
        }} />
        <div className="lcl-quotas"><Quota label="5 小时额度" window={profile.usage.fiveHour} usage={profile.usage} now={now} /><Quota label="7 天额度" window={profile.usage.sevenDay} usage={profile.usage} now={now} /></div>
        <div className={`lcl-freshness ${freshness === 'stale' || anyExpired ? 'is-stale' : ''}`}><Clock3 size={13} /><div><span>{freshness === 'never' ? '尚未采集用量' : `采集于 ${dateTime(profile.usage.observedAt)}`}{freshness === 'stale' || anyExpired ? ' · 已过期' : ''}</span><small>{sourceLabel(profile.usage)}</small></div></div>
        <dl className="lcl-facts"><ExtraUsage usage={profile.usage} /><dt>重置次数</dt><dd>未知 <button className="lcl-text-button" disabled={writeDisabled} title={readonly ? '请在 Cockpit 应用中操作' : undefined} onClick={() => act(profile, 'open_usage')}>官方用量页<ExternalLink size={11} /></button></dd>{profile.renewalDate && <><dt>续费提醒</dt><dd>{profile.renewalDate}<span className="lcl-detail-secondary">你记录的日期</span></dd></>}</dl>
        {profile.usage.error && <p className="lcl-inline-error"><AlertCircle size={14} />{profile.usage.error}</p>}
        <p className="lcl-quiet-note">{collectorStatus(profile, now)}</p>
        {pending && <div className="lcl-login-pending"><strong>等待你完成官方 CLI 登录</strong><span>授权后点击刷新确认；Desktop 需要单独登录。</span></div>}
        {!readonly && <div className="lcl-account-actions">
          <button className="btn btn-primary" disabled={disabled || suspended} onClick={() => { if (profile.health?.status === 'auth_required') act(profile, 'login_cli'); else if (profile.kind === 'current' && profile.auth.status !== 'signed_in') void inspectAuth(profile); else act(profile, profile.auth.status === 'signed_in' ? 'launch_cli' : 'login_cli'); }}><Terminal size={14} />{profile.health?.status === 'auth_required' ? '官方 CLI 登录' : profile.kind === 'current' && profile.auth.status !== 'signed_in' ? '检查当前身份' : profile.auth.status === 'signed_in' ? '启动 CLI' : '官方 CLI 登录'}</button>
          <button className="btn btn-secondary" disabled={disabled || desktopBusy || suspended} title={desktopBusy ? '现有 Desktop 或 Code 尚在运行，不会关闭它们' : '打开独立 Desktop 配置，需单独完成官方登录'} onClick={() => act(profile, 'launch_desktop')}><Monitor size={14} />启动 Desktop</button>
          <button className="btn btn-secondary" disabled={disabled || suspended || now < retryAt} onClick={() => void run('刷新账号', async () => applyOverview(await service.refresh(profile.id), true), 'refresh')} aria-label={`刷新${profile.label}`}><RefreshCw size={14} />刷新</button>
        </div>}
        <details className="lcl-account-details"><summary>账号设置<ChevronDown size={14} /></summary>
          <div className="lcl-account-settings">
            <button className="btn btn-secondary" disabled={writeDisabled || selected} onClick={() => void run('设置下次默认账号', async () => { applyOverview(await service.select(profile.id)); setNotice({ kind: 'info', text: `已选择“${profile.label}”，只影响下次从本工具启动，不更改全局命令或现有会话。` }); })}>{selected ? '已是下次默认账号' : '设为下次默认账号'}</button>
            {!readonly && profile.auth.status === 'signed_in' && profile.kind !== 'current' && <button className="lcl-text-button" disabled={disabled} onClick={() => act(profile, 'login_cli')}>重新打开官方登录</button>}
            <div className="lcl-collector"><span>{profile.collectorInstalled ? '状态栏采集已启用' : '状态栏采集未启用'}</span><button className="lcl-text-button" disabled={writeDisabled || profile.collectorInstalled} onClick={() => act(profile, 'install_collector')}>{profile.collectorInstalled ? '已启用' : '启用采集'}</button><small>已有自定义状态栏时会提示冲突，不会覆盖。</small></div>
            <ProfilePreferences profile={profile} disabled={writeDisabled} onSave={async (autoRefresh, renewalDate) => { await run('保存提醒设置', async () => applyOverview(await service.preferences(profile.id, autoRefresh, renewalDate))); }} />
            <details className="lcl-paths"><summary>配置位置</summary><span>CLI</span><code>{profile.configDir}</code><span>Desktop</span><code>{profile.desktopDir}</code></details>
            <div><button className="btn btn-secondary" disabled={writeDisabled || selected} title={selected ? '当前默认账号不能移除' : '保留数据、凭据与所有回执，只从正常列表移除'} onClick={() => void archiveProfile(profile.id, true)}>从列表移除（保留全部数据）</button><p className="lcl-quiet-note">移除后停止此配置的管理器查询与工作入口。会话、备份及认证保留，可恢复。</p></div>
          </div>
        </details>
      </article>;
    })}</div>}

    {!loading && (overview.archivedProfiles?.length ?? 0) > 0 && <details className="lcl-sessions"><summary>已从列表移除的账号（{overview.archivedProfiles!.length}）</summary>
      <p className="lcl-quiet-note">这里只显示归档元数据，不查询这些账号。恢复不会切号、重新登录或重新开启自动查询。</p>
      {overview.archivedProfiles!.map((profile) => <div className="lcl-session-row" key={profile.id}><div><strong>{profile.label}</strong><p>归档于 {dateTime(profile.archivedAt)}</p></div><button className="btn btn-secondary" disabled={writeDisabled} onClick={() => void archiveProfile(profile.id, false)}>恢复到列表</button></div>)}
    </details>}
    {!loading && (sorted.length > 1 || (overview.archivedProfiles?.length ?? 0) > 0) && <LocalClaudeSwitch service={service} overview={overview} readonly={readonly} externalBusy={disabled} onUpdated={applyOverview} onBusyChange={switchBusy} />}
    {!loading && sorted.length > 0 && <section className="lcl-sessions" aria-label="本地会话保全与续接">
      <header className="lcl-sessions-header"><div><h2>本地会话</h2><p>切换后按保存记录继续，正在执行的任务先结束。</p></div><div className="lcl-actions">
        <button className="btn btn-secondary" disabled={disabled || sessionsLoading || !sessionSourceId} onClick={() => void loadSessions(sessionSourceId, overview.selectedProfileId)}><RefreshCw size={14} />刷新会话</button>
        <button className="btn btn-secondary" disabled={writeDisabled || !sessionSourceId} title={readonly ? '请在 Cockpit 应用中操作' : '备份完整原始记录，不删除或截断正在追加的文件'} onClick={() => void preserveSessions()}><Archive size={14} />备份全部会话</button>
      </div></header>
      <div className="lcl-session-selectors"><label>读取账号<select value={sessionSourceId} onChange={(event) => setSessionSourceId(event.target.value)} disabled={disabled}>{overview.profiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.label}</option>)}</select></label><span>继续使用：<strong>{overview.profiles.find((profile) => profile.id === overview.selectedProfileId)?.label ?? '请先选择下次默认账号'}</strong></span></div>
      {sessionsError && <p className="lcl-inline-error" role="alert"><AlertCircle size={14} />{sessionsError}</p>}
      {sessionInventory && <div className="lcl-session-summary"><span>{sessionInventory.sessions.length} 个会话，{sessionInventory.summary.files} 个记录文件，{formatSessionBytes(sessionInventory.summary.bytes)}</span><span>检查于 {dateTime(sessionInventory.checkedAt)}</span>{(!sessionInventory.activityKnown || sessionInventory.active) && <strong>{sessionInventory.activityKnown ? '源配置仍有相关进程，关闭原会话后继续' : '活动状态未知，续接暂不可用'}</strong>}</div>}
      {backup ? <div className="lcl-snapshot-result"><Archive size={15} /><div><strong>{backup.ok ? '已保全原始记录' : '最近备份未完成'}</strong><span>{backup.files} 个文件，{formatSessionBytes(backup.bytes)}；写入变化 {backup.unstableFiles}，不完整 {backup.incompleteFiles}，需检查 {backup.invalidFiles}</span><details><summary>备份位置</summary><code>{backup.backupPath}</code></details></div></div> : <p className="lcl-quiet-note lcl-backup-note">尚未读取到备份回执；本地原始记录保持不变。</p>}
      {sessionsLoading && !sessionInventory ? <p className="lcl-session-loading" role="status"><Loader2 size={16} className="lcl-spin" />正在读取本地保存记录…</p> : sessionInventory?.sessions.length === 0 ? <p className="lcl-session-loading">该配置下尚未找到可显示的会话记录。</p> : <div className="lcl-session-list">{sessionInventory?.sessions.slice().sort((a, b) => b.updatedAt - a.updatedAt).map((session) => {
        const blocked = !overview.selectedProfileId ? '请先选择下次默认账号' : overview.profiles.find((p) => p.id === overview.selectedProfileId)?.health?.status === 'account_disabled' ? '目标账号已停用，不能启动续接' : sessionResumeBlockReason(sessionInventory, targetInventory, session);
        const parseLabel = ['ok', 'valid', 'complete'].includes(session.parseStatus) ? '记录可解析' : session.parseStatus === 'incomplete' ? '记录尚未写完整' : '记录需要检查';
        return <article className="lcl-session-row" key={session.sessionId}>
          <div className="lcl-session-main"><h3>{session.label || session.projectKey || '未命名会话'}</h3><p><FolderOpen size={13} /><span>{session.cwd || session.projectKey || '项目未知'}</span></p><div className="lcl-session-meta"><span>保存于 {dateTime(session.updatedAt)}</span><span className={session.active ? 'lcl-session-active' : ''}>{sessionInventory.activityKnown ? sessionActivityLabel(session.activity) : '状态待确认'}</span><span>{parseLabel}{!session.stable ? '，尚未稳定' : ''}</span><span>{session.files} 个文件</span></div></div>
          <div className="lcl-session-resume"><button className="btn btn-secondary" disabled={writeDisabled || sessionsLoading || !!blocked} title={readonly ? '请在 Cockpit 应用中操作' : blocked ?? '通过官方 CLI，在原项目中按保存记录继续'} onClick={() => void resumeSession(session)}><Terminal size={14} />从所选账号继续</button>{blocked && <small>{blocked}</small>}</div>
        </article>;
      })}</div>}
      <p className="lcl-session-footnote">这里只续接官方 CLI 会话，不重写 Desktop 历史索引，也不会关闭当前任务。</p>
    </section>}

    {!loading && sorted.length > 0 && <div className="lcl-footer"><button className="lcl-text-button" disabled={writeDisabled || overview.profiles.some((profile) => profile.kind === 'current')} onClick={() => void run('关联原有配置', async () => { applyOverview(await service.registerCurrent()); setNotice({ kind: 'info', text: '原有配置已关联，没有复制凭据。' }); })}>{overview.profiles.some((profile) => profile.kind === 'current') ? '原有配置已关联' : '关联原有配置'}</button><span>{readonly ? '这里仅展示本地已有摘要，刷新不会访问账号服务。' : '每 30 秒读取各配置的本地摘要；官方用量查询只对符合条件的空闲账号进行。关闭管理器后不监控。'}</span></div>}
    <details className="lcl-boundaries"><summary>登录与用量说明<ChevronDown size={14} /></summary><ul><li>CLI 与 Desktop 分别完成官方登录；本工具不复制登录凭据，也不自动迁移工作会话。</li><li>额度来自官方客户端采集。空闲、繁忙或查询失败时可能过期；不会为刷新发送模型提示。</li><li>官方用量页使用系统默认浏览器，请核对浏览器中登录的账号。重置和付费操作只在官方页面完成。</li><li>客户端沿用本机网络设置；检查客户端用于确认本地安装，不验证账号或服务端可用性。</li></ul></details>
  </section>;
}
