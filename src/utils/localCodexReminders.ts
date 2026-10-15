import { creditsToUsd, formatCredits, normalizeCodexPlanType, PRO_GRANT_EXPIRES_AT } from './localCodexPlans';

export interface LocalCodexQuota {
  hourly_percentage: number;
  hourly_reset_time?: number | null;
  hourly_window_present?: boolean | null;
  hourly_window_minutes?: number | null;
  weekly_percentage: number;
  weekly_reset_time?: number | null;
  weekly_window_present?: boolean | null;
  weekly_window_minutes?: number | null;
  reset_credits_available?: number | null;
  reset_credits_next_expires_at?: number | null;
}

export interface LocalCodexUsageCredits {
  hasCredits: boolean;
  unlimited: boolean;
  balance?: number | null;
}

export interface LocalCodexAccount {
  id: string;
  email: string;
  planType?: string | null;
  subscriptionActiveUntil?: string | null;
  quota?: LocalCodexQuota | null;
  usageCredits?: LocalCodexUsageCredits | null;
  quotaError?: string | null;
  usageUpdatedAt?: number | null;
  requiresReauth: boolean;
  reauthReason?: string | null;
  accessTokenExpiresAt?: number | null;
  hasRefreshToken: boolean;
  isCurrent: boolean;
  isLiveLocal: boolean;
  lastUsed: number;
  createdAt: number;
}

export interface Reminder {
  key: string;
  title: string;
  body: string;
}

export const SUBSCRIPTION_WARN_DAYS = 3;
export const WEEKLY_LOW_PERCENT = 10;
/** About $100 at the official top-up price. */
export const CREDITS_LOW_BALANCE = 2_500;
export const GRANT_EXPIRY_WARN_DAYS = [14, 3];

const DAY_MS = 86_400_000;
const WEEK_MINUTES = 7 * 24 * 60;

export interface QuotaWindow {
  label: string;
  percent: number;
  resetTime?: number | null;
  minutes?: number | null;
}

export function windowLabel(minutes?: number | null, fallback = ''): string {
  if (!minutes) return fallback;
  if (minutes >= WEEK_MINUTES * 0.9) return '每周';
  if (minutes % 60 === 0) return `${minutes / 60} 小时`;
  return `${minutes} 分钟`;
}

/**
 * The windows the API actually returned. Since July 2026 some Pro accounts only get the
 * weekly window, delivered in the primary slot, so labels come from the window length.
 */
export function quotaWindows(quota?: LocalCodexQuota | null): QuotaWindow[] {
  if (!quota) return [];
  const windows: QuotaWindow[] = [];
  if (quota.hourly_window_present !== false) {
    windows.push({ label: windowLabel(quota.hourly_window_minutes, '5 小时'), percent: quota.hourly_percentage, resetTime: quota.hourly_reset_time, minutes: quota.hourly_window_minutes });
  }
  if (quota.weekly_window_present !== false) {
    windows.push({ label: windowLabel(quota.weekly_window_minutes, '每周'), percent: quota.weekly_percentage, resetTime: quota.weekly_reset_time, minutes: quota.weekly_window_minutes });
  }
  return windows;
}

export function weeklyWindow(quota?: LocalCodexQuota | null): QuotaWindow | null {
  return quotaWindows(quota).find((window) => window.label === '每周') ?? null;
}

/** Upstream stores the expiry either as an ISO string or as unix seconds. */
export function parseSubscriptionExpiry(value?: string | null): number | null {
  if (!value) return null;
  const trimmed = value.trim();
  if (/^\d+(\.\d+)?$/.test(trimmed)) {
    const numeric = Number(trimmed);
    return numeric > 1e12 ? numeric : numeric * 1000;
  }
  const parsed = Date.parse(trimmed);
  return Number.isNaN(parsed) ? null : parsed;
}

export function creditBalance(account?: LocalCodexAccount | null): number | null {
  const credits = account?.usageCredits;
  if (!credits || credits.unlimited) return null;
  return typeof credits.balance === 'number' && Number.isFinite(credits.balance) ? credits.balance : null;
}

export function daysUntil(targetMs: number, nowMs: number): number {
  return Math.ceil((targetMs - nowMs) / DAY_MS);
}

function localDay(nowMs: number): string {
  const date = new Date(nowMs);
  return `${date.getFullYear()}-${date.getMonth() + 1}-${date.getDate()}`;
}

/**
 * Reminders due for the given snapshot. Each key is stable for the period it covers, so the
 * caller can drop keys it has already shown.
 */
export function collectReminders(
  accounts: LocalCodexAccount[],
  previous: Map<string, LocalCodexAccount>,
  nowMs: number,
): Reminder[] {
  const reminders: Reminder[] = [];
  for (const account of accounts) {
    const expiry = parseSubscriptionExpiry(account.subscriptionActiveUntil);
    if (expiry !== null) {
      const days = daysUntil(expiry, nowMs);
      if (days <= SUBSCRIPTION_WARN_DAYS) {
        reminders.push({
          key: `subscription:${account.id}:${localDay(nowMs)}`,
          title: days <= 0 ? 'Codex 订阅已到期' : 'Codex 订阅快到期',
          body: days <= 0
            ? `${account.email} 的订阅已经到期，需要续费。`
            : `${account.email} 的订阅还有 ${days} 天到期，记得续费。`,
        });
      }
    }

    const balance = creditBalance(account);
    const balanceBefore = creditBalance(previous.get(account.id));
    if (balance !== null && balanceBefore !== null && balance < balanceBefore - 0.5) {
      reminders.push({
        key: `credits-spend:${account.id}:${localDay(nowMs)}`,
        title: 'Codex 正在消耗积分',
        body: `${account.email} 的套餐额度已用完，开始扣积分：刚才用掉 ${formatCredits(balanceBefore - balance)}，还剩 ${formatCredits(balance)}（约 $${creditsToUsd(balance).toLocaleString('en-US')}）。`,
      });
    }
    if (balance !== null && balanceBefore !== null && balance <= CREDITS_LOW_BALANCE && balanceBefore > CREDITS_LOW_BALANCE) {
      reminders.push({
        key: `credits-low:${account.id}:${localDay(nowMs)}`,
        title: 'Codex 积分快用完了',
        body: `${account.email} 只剩 ${formatCredits(balance)} 积分（约 $${creditsToUsd(balance).toLocaleString('en-US')}）。`,
      });
    }
    if (balance !== null && balance > 0 && normalizeCodexPlanType(account.planType) === 'pro' && nowMs < PRO_GRANT_EXPIRES_AT) {
      const daysLeft = daysUntil(PRO_GRANT_EXPIRES_AT, nowMs);
      const bucket = GRANT_EXPIRY_WARN_DAYS.filter((days) => daysLeft <= days).pop();
      if (bucket !== undefined) {
        reminders.push({
          key: `credits-expiry:${account.id}:${bucket}`,
          title: 'Pro 补偿积分快过期了',
          body: `${account.email} 还有 ${formatCredits(balance)} 积分，补偿积分 12 月 31 日过期，还剩 ${daysLeft} 天。`,
        });
      }
    }

    const weekly = weeklyWindow(account.quota);
    if (!weekly) continue;
    if ((account.isCurrent || account.isLiveLocal) && weekly.percent <= WEEKLY_LOW_PERCENT) {
      const fallback = balance !== null && balance > 0
        ? `用完后会开始扣积分（剩 ${formatCredits(balance)}）`
        : '可以换号或用重置卡';
      reminders.push({
        key: `weekly-low:${account.id}:${weekly.resetTime ?? 'unknown'}`,
        title: '当前 Codex 账号周额度快用完了',
        body: `${account.email} 周额度只剩 ${weekly.percent}%，${fallback}。`,
      });
    }

    const before = weeklyWindow(previous.get(account.id)?.quota);
    if (before && before.resetTime && before.resetTime * 1000 <= nowMs && weekly.percent > before.percent) {
      reminders.push({
        key: `weekly-reset:${account.id}:${before.resetTime}`,
        title: 'Codex 周额度已刷新',
        body: `${account.email} 的周额度已经重置，现在剩 ${weekly.percent}%。`,
      });
    }
  }
  return reminders;
}
