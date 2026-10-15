import type { ClaudeProfile, ClaudeUsage, WindowUsage } from '../types/localClaude';

export const CLAUDE_AUTO_REFRESH_MS = 30 * 60 * 1000;
export const CLAUDE_STALE_AFTER_MS = 35 * 60 * 1000;
export interface ClaudeReminder { key: string; title: string; body: string }
export type UsageFreshness = 'never' | 'fresh' | 'stale';

export function usedPercent(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? Math.min(100, value) : null;
}

export function usageFreshness(usage: ClaudeUsage, nowMs: number): UsageFreshness {
  const observed = usage.observedAt;
  if (observed === null || !Number.isFinite(observed) || observed <= 0 || usage.source === 'none') return 'never';
  if ((usage.error && usage.errorCode !== 'busy') || observed * 1000 > nowMs + 60_000 || nowMs - observed * 1000 >= CLAUDE_STALE_AFTER_MS) return 'stale';
  return 'fresh';
}

export function canAutoRefreshClaude(profile: ClaudeProfile): boolean {
  return profile.autoRefresh && profile.auth.status === 'signed_in'
    && !['account_disabled', 'auth_required'].includes(profile.health?.status ?? '')
    && !['account_disabled', 'auth_expired', 'auth_required', 'identity_mismatch'].includes(profile.usage.errorCode ?? '');
}

export function healthLabel(profile: ClaudeProfile, nowMs: number): string {
  const health = profile.health;
  if (!health || health.status === 'unknown') return '可用性尚未核验';
  if (health.status === 'account_disabled') return '官方明确停用；账号请求已暂停';
  if (health.status === 'auth_required') return '官方提示需要重新登录；不等于封禁';
  if (health.status === 'network_error') return '最近查询网络失败；账号状态未知';
  if (health.status === 'unavailable') return '最近检查未完成；账号状态未知';
  const stale = !health.observedAt || nowMs - health.observedAt * 1000 >= CLAUDE_STALE_AFTER_MS;
  return stale ? '上次官方用量服务可用；证据已过期' : '最近官方用量服务可用；未验证新模型请求';
}

export function collectorStatus(profile: ClaudeProfile, nowMs: number): string {
  if (!profile.collectorInstalled) return '管理器未安装状态栏采集；在用时只读取已有本地缓存。';
  if (profile.usage.source === 'official_statusline' && usageFreshness(profile.usage, nowMs) === 'fresh') return '最近收到官方状态栏样本。';
  return '状态栏回调已配置；尚无近期状态栏样本。';
}

export function windowExpired(window: WindowUsage | null, nowMs: number): boolean {
  return window?.resetsAt != null && Number.isFinite(window.resetsAt) && window.resetsAt * 1000 <= nowMs;
}

/** A date typed by the user, not a credential expiry or verified billing date. */
export function renewalDays(date: string | null, nowMs: number): number | null {
  if (!date || !/^\d{4}-\d{2}-\d{2}$/.test(date)) return null;
  const [year, month, day] = date.split('-').map(Number);
  const target = new Date(year, month - 1, day);
  if (target.getFullYear() !== year || target.getMonth() !== month - 1 || target.getDate() !== day) return null;
  const now = new Date(nowMs);
  return Math.round((Date.UTC(year, month - 1, day) - Date.UTC(now.getFullYear(), now.getMonth(), now.getDate())) / 86_400_000);
}

export function formatMinorAmount(value: number | null, currency: string | null): string | null {
  if (value === null || !Number.isFinite(value) || value < 0 || !currency || !/^[A-Z]{3}$/.test(currency)) return null;
  // Only currencies whose minor-unit convention is known to this client are formatted.
  const digits: Record<string, number> = { USD: 2, EUR: 2, GBP: 2, CNY: 2, CAD: 2, AUD: 2, HKD: 2, SGD: 2, JPY: 0, KRW: 0, BHD: 3, KWD: 3 };
  if (!(currency in digits)) return null;
  return new Intl.NumberFormat('zh-CN', { style: 'currency', currency, currencyDisplay: 'code', minimumFractionDigits: digits[currency], maximumFractionDigits: digits[currency] }).format(value / 10 ** digits[currency]);
}

export function refreshRetryDelay(failures: number): number {
  return Math.min(120_000, 5_000 * 2 ** Math.max(0, Math.min(5, failures - 1)));
}

export function collectClaudeReminders(profiles: ClaudeProfile[], nowMs: number): ClaudeReminder[] {
  const reminders: ClaudeReminder[] = [];
  for (const profile of profiles) {
    const days = renewalDays(profile.renewalDate, nowMs);
    if (days !== null && days >= 0 && days <= 3) {
      reminders.push({ key: `renewal:${profile.id}:${profile.renewalDate}`, title: 'Claude 续费日期提醒', body: `${profile.label}：你记录的续费日期${days === 0 ? '是今天' : `还有 ${days} 天`}，可在官方账单页核对。` });
    }
    if (profile.auth.status !== 'signed_in' || usageFreshness(profile.usage, nowMs) !== 'fresh') continue;
    for (const [key, label, window] of [['fiveHour', '5 小时', profile.usage.fiveHour], ['sevenDay', '7 天', profile.usage.sevenDay]] as const) {
      const percent = usedPercent(window?.usedPercent);
      if (percent === null || percent < 90 || windowExpired(window, nowMs)) continue;
      const period = window?.resetsAt ?? Math.floor((profile.usage.observedAt ?? 0) / 86_400);
      const level = percent >= 100 ? 'limit' : 'near';
      reminders.push({ key: `quota:${profile.id}:${key}:${period}:${level}`, title: percent >= 100 ? 'Claude 额度已达上限' : 'Claude 额度快用完了', body: `${profile.label} 的 ${label}额度已用 ${percent}%。请在官方用量页查看可用选项。` });
    }
  }
  return reminders;
}

/** Stores deduplication keys only; no profile, email, credential or notification body. */
export function takeUnseenClaudeReminders(reminders: ClaudeReminder[], shown: Set<string>): ClaudeReminder[] {
  const fresh = reminders.filter((reminder) => {
    if (shown.has(reminder.key)) return false;
    shown.add(reminder.key);
    return true;
  });
  while (shown.size > 300) shown.delete(shown.values().next().value!);
  return fresh;
}

export function sessionResumeBlockReason(
  source: import('../types/localClaude').ClaudeSessionInventory | null,
  target: import('../types/localClaude').ClaudeSessionInventory | null,
  session: import('../types/localClaude').ClaudeSession,
): string | null {
  if (!source || !target || !source.activityKnown || !target.activityKnown) return '运行状态尚未确认，请先刷新会话';
  if (source.active || session.active) return '源配置仍有相关进程，请先关闭原会话';
  if (target.active) return '所选账号仍有相关进程，请先关闭相关会话';
  if (!session.stable) return '记录尚未稳定，请稍后刷新再续接';
  if (!['ok', 'valid', 'complete'].includes(session.parseStatus)) return '会话记录需要检查，暂不能续接';
  return null;
}

export function formatSessionBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '大小未知';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function sessionActivityLabel(activity: string): string {
  if (activity === 'active') return '会话进程仍在';
  if (activity === 'possibly_active') return '配置正在使用';
  if (['idle', 'inactive', 'not_active'].includes(activity)) return '未检测到会话进程';
  return '状态待确认';
}
