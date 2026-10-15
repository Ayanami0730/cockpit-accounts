import assert from 'node:assert/strict';
import test from 'node:test';
import {
  collectReminders,
  daysUntil,
  parseSubscriptionExpiry,
  quotaWindows,
  weeklyWindow,
  windowLabel,
  type LocalCodexAccount,
} from './localCodexReminders';

const NOW = Date.parse('2026-09-23T12:00:00+08:00');

function account(overrides: Partial<LocalCodexAccount> = {}): LocalCodexAccount {
  return {
    id: 'fixture-a',
    email: 'fixture@example.invalid',
    requiresReauth: false,
    hasRefreshToken: true,
    isCurrent: false,
    isLiveLocal: false,
    lastUsed: 0,
    createdAt: 0,
    ...overrides,
  };
}

test('subscription expiry accepts ISO strings and unix seconds', () => {
  assert.equal(parseSubscriptionExpiry('2026-09-25T12:00:00+08:00'), Date.parse('2026-09-25T12:00:00+08:00'));
  assert.equal(parseSubscriptionExpiry('1790000000'), 1_790_000_000_000);
  assert.equal(parseSubscriptionExpiry(''), null);
  assert.equal(parseSubscriptionExpiry('not a date'), null);
  assert.equal(daysUntil(NOW + 2.5 * 86_400_000, NOW), 3);
});

test('subscription reminder fires within three days, once per local day', () => {
  const soon = account({ subscriptionActiveUntil: new Date(NOW + 2 * 86_400_000).toISOString() });
  const later = account({ id: 'fixture-b', subscriptionActiveUntil: new Date(NOW + 10 * 86_400_000).toISOString() });
  const reminders = collectReminders([soon, later], new Map(), NOW);
  assert.equal(reminders.length, 1);
  assert.match(reminders[0].body, /还有 2 天/);
  const sameDay = collectReminders([soon], new Map(), NOW + 3_600_000);
  assert.equal(sameDay[0].key, reminders[0].key);
});

test('expired subscription says so', () => {
  const expired = account({ subscriptionActiveUntil: new Date(NOW - 86_400_000).toISOString() });
  assert.equal(collectReminders([expired], new Map(), NOW)[0].title, 'Codex 订阅已到期');
});

test('low weekly quota only warns for the current account', () => {
  const quota = { hourly_percentage: 50, weekly_percentage: 8, weekly_reset_time: 1_790_000_000 };
  assert.equal(collectReminders([account({ quota })], new Map(), NOW).length, 0);
  const current = collectReminders([account({ quota, isCurrent: true })], new Map(), NOW);
  assert.equal(current.length, 1);
  assert.equal(current[0].key, 'weekly-low:fixture-a:1790000000');
});

test('a weekly-only account delivered in the primary slot is read as weekly', () => {
  const quota = {
    hourly_percentage: 8,
    hourly_window_minutes: 10080,
    hourly_reset_time: 1_790_000_000,
    weekly_percentage: 0,
    weekly_window_present: false,
  };
  assert.deepEqual(quotaWindows(quota).map((window) => window.label), ['每周']);
  assert.equal(weeklyWindow(quota)?.percent, 8);
  const live = collectReminders([account({ quota, isLiveLocal: true })], new Map(), NOW);
  assert.equal(live.length, 1);
  assert.match(live[0].body, /只剩 8%/);
  assert.equal(windowLabel(300), '5 小时');
  assert.equal(windowLabel(undefined, '5 小时'), '5 小时');
});

function withCredits(balance: number | null, overrides: Partial<LocalCodexAccount> = {}): LocalCodexAccount {
  return account({ usageCredits: { hasCredits: balance !== null && balance > 0, unlimited: false, balance }, ...overrides });
}

test('a falling credit balance reports spending once per day', () => {
  const before = withCredits(62_500);
  const after = withCredits(61_250);
  const reminders = collectReminders([after], new Map([[before.id, before]]), NOW);
  assert.equal(reminders.length, 1);
  assert.equal(reminders[0].title, 'Codex 正在消耗积分');
  assert.match(reminders[0].body, /用掉 1,250，还剩 61,250（约 \$2,450）/);
  assert.equal(collectReminders([before], new Map([[before.id, before]]), NOW).length, 0);
  assert.equal(collectReminders([after], new Map(), NOW).length, 0);
});

test('crossing the low-credit line warns, unlimited balances are ignored', () => {
  const before = withCredits(3_000);
  const after = withCredits(2_000);
  const titles = collectReminders([after], new Map([[before.id, before]]), NOW).map((r) => r.title);
  assert.deepEqual(titles, ['Codex 正在消耗积分', 'Codex 积分快用完了']);
  const unlimited = account({ usageCredits: { hasCredits: true, unlimited: true, balance: 10 } });
  assert.equal(collectReminders([unlimited], new Map([[unlimited.id, before]]), NOW).length, 0);
});

test('Pro grant expiry warns at 14 and 3 days left, only for Pro with a balance', () => {
  const dec20 = new Date(2026, 11, 20, 12).getTime();
  const dec29 = new Date(2026, 11, 29, 12).getTime();
  const pro = withCredits(40_000, { planType: 'pro' });
  assert.equal(collectReminders([pro], new Map(), NOW).length, 0);
  assert.equal(collectReminders([pro], new Map(), dec20)[0].key, 'credits-expiry:fixture-a:14');
  assert.equal(collectReminders([pro], new Map(), dec29)[0].key, 'credits-expiry:fixture-a:3');
  assert.equal(collectReminders([withCredits(40_000, { planType: 'plus' })], new Map(), dec29).length, 0);
  assert.equal(collectReminders([withCredits(0, { planType: 'pro' })], new Map(), dec29).length, 0);
});

test('weekly-low warning mentions credits when there is a balance', () => {
  const quota = { hourly_percentage: 50, weekly_percentage: 8, weekly_reset_time: 1_790_000_000 };
  const reminders = collectReminders([withCredits(62_500, { quota, isLiveLocal: true })], new Map(), NOW);
  assert.match(reminders[0].body, /开始扣积分（剩 62,500）/);
});

test('weekly reset is reported when the old window passed and remaining quota went up', () => {
  const resetAt = Math.floor(NOW / 1000) - 60;
  const before = account({ quota: { hourly_percentage: 0, weekly_percentage: 5, weekly_reset_time: resetAt } });
  const after = account({ quota: { hourly_percentage: 100, weekly_percentage: 100, weekly_reset_time: resetAt + 7 * 86_400 } });
  const reminders = collectReminders([after], new Map([[before.id, before]]), NOW);
  assert.equal(reminders.length, 1);
  assert.equal(reminders[0].title, 'Codex 周额度已刷新');
  const notYet = account({ quota: { hourly_percentage: 0, weekly_percentage: 5, weekly_reset_time: resetAt + 3600 } });
  assert.equal(collectReminders([after], new Map([[notYet.id, notYet]]), NOW).length, 0);
});
