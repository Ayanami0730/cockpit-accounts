/**
 * Codex plan types as returned by ChatGPT (`plan_type` in wham/usage and `chatgpt_plan_type`
 * in the id_token). The value set mirrors `PlanType` in openai/codex `protocol/src/account.rs`.
 *
 * OpenAI publishes only relative multipliers (Pro 5x / Pro 20x of Plus) and no weekly cap.
 * The dollar range is a community estimate: two 2026 Reddit measurements of a fully used Plus
 * week at API list prices (~$55 with cache discount, ~$134), scaled by the official multiplier.
 */
export interface CodexPlanInfo {
  key: string;
  label: string;
  monthlyUsd: number | null;
  multiplier: number | null;
  weeklyUsd: [number, number] | null;
  note: string;
}

const PLUS_WEEKLY_USD: [number, number] = [55, 135];

function scaled(multiplier: number): [number, number] {
  return [Math.round(PLUS_WEEKLY_USD[0] * multiplier), Math.round(PLUS_WEEKLY_USD[1] * multiplier)];
}

const PLANS: Record<string, Omit<CodexPlanInfo, 'key'>> = {
  free: { label: 'Free', monthlyUsd: 0, multiplier: 0.05, weeklyUsd: scaled(0.05), note: '官方未公布额度，按约 Plus 的 5% 推测，不能用 Sol' },
  go: { label: 'Go', monthlyUsd: 8, multiplier: 0.125, weeklyUsd: scaled(0.125), note: '官方未公布额度，按约 Plus 的 12.5% 推测，不能用 Sol' },
  plus: { label: 'Plus', monthlyUsd: 20, multiplier: 1, weeklyUsd: scaled(1), note: '基准档' },
  prolite: { label: 'Pro 5x', monthlyUsd: 100, multiplier: 5, weeklyUsd: scaled(5), note: '官方：Plus 的 5 倍' },
  pro: { label: 'Pro 20x', monthlyUsd: 200, multiplier: 20, weeklyUsd: scaled(20), note: '官方：Plus 的 20 倍，10-29 后降为 10 倍；社区另有一条 6 月实测折合约 $560/周' },
  team: { label: 'Team', monthlyUsd: null, multiplier: 1, weeklyUsd: scaled(1), note: '工作区席位，官方按 Plus 同等额度' },
  business: { label: 'Business', monthlyUsd: null, multiplier: 1, weeklyUsd: scaled(1), note: '标准席位约等于 Plus；Premium 席位是 5 倍，接口不区分' },
  self_serve_business_usage_based: { label: 'Business 按量', monthlyUsd: null, multiplier: null, weeklyUsd: null, note: '按 credit 计费，没有固定周额度' },
  enterprise: { label: 'Enterprise', monthlyUsd: null, multiplier: 1, weeklyUsd: scaled(1), note: '非弹性计费时按 Plus 同等额度；弹性计费按 credit，没有固定上限' },
  enterprise_cbp_usage_based: { label: 'Enterprise 按量', monthlyUsd: null, multiplier: null, weeklyUsd: null, note: '按 credit 计费，没有固定周额度' },
  edu: { label: 'Edu', monthlyUsd: null, multiplier: 1, weeklyUsd: scaled(1), note: '非弹性计费时按 Plus 同等额度' },
};

export function normalizeCodexPlanType(planType?: string | null): string {
  const value = (planType ?? '').trim().toLowerCase().replace(/[\s-]+/g, '_');
  if (!value) return 'unknown';
  if (value === 'pro_lite' || value === 'pro_5x' || value === 'codex_pro_5x') return 'prolite';
  if (value === 'pro_max' || value === 'promax' || value === 'pro_20x' || value === 'codex_pro_20x') return 'pro';
  return value;
}

/** From this local date, Pro $200 drops from 20x to 10x Plus (OpenAI notice, 2026-09-29). */
export const PRO_10X_FROM = new Date(2026, 9, 30).getTime();

const PRO_10X: Omit<CodexPlanInfo, 'key'> = {
  label: 'Pro 10x',
  monthlyUsd: 200,
  multiplier: 10,
  weeklyUsd: scaled(10),
  note: '官方：2026-10-30 起 Pro $200 降为 Plus 的 10 倍（按 API 价计约为原 20x 的一半）',
};

export function getCodexPlanInfo(planType?: string | null, nowMs = Date.now()): CodexPlanInfo {
  const key = normalizeCodexPlanType(planType);
  if (key === 'pro' && nowMs >= PRO_10X_FROM) return { key, ...PRO_10X };
  const known = PLANS[key];
  if (known) return { key, ...known };
  return {
    key,
    label: key === 'unknown' ? '未知' : key,
    monthlyUsd: null,
    multiplier: null,
    weeklyUsd: null,
    note: '未识别的账号类型，没有额度估算',
  };
}

export function formatUsdRange(range: [number, number]): string {
  return `$${range[0].toLocaleString('en-US')}–${range[1].toLocaleString('en-US')}`;
}

/** Official top-up price: 1,000 credits for $40. */
export const USD_PER_CREDIT = 0.04;
/** The one-time 62,500-credit grant to existing Pro $200 subscribers expires at the end of this local day. */
export const PRO_GRANT_EXPIRES_AT = new Date(2026, 11, 31, 23, 59, 59).getTime();

export function creditsToUsd(credits: number): number {
  return Math.round(credits * USD_PER_CREDIT * 100) / 100;
}

export function formatCredits(credits: number): string {
  return credits.toLocaleString('en-US', { maximumFractionDigits: 2 });
}

/** Estimated dollars left this week, from the remaining weekly percentage. */
export function remainingWeeklyUsd(info: CodexPlanInfo, weeklyRemainingPercent?: number | null): [number, number] | null {
  if (!info.weeklyUsd || weeklyRemainingPercent === null || weeklyRemainingPercent === undefined) return null;
  const ratio = Math.max(0, Math.min(100, weeklyRemainingPercent)) / 100;
  return [Math.round(info.weeklyUsd[0] * ratio), Math.round(info.weeklyUsd[1] * ratio)];
}
