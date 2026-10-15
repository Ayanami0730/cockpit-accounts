import assert from 'node:assert/strict';
import test from 'node:test';
import type { ClaudeProfile, ClaudeUsage } from '../types/localClaude';
import { CLAUDE_STALE_AFTER_MS, canAutoRefreshClaude, collectorStatus, healthLabel, collectClaudeReminders, formatMinorAmount, refreshRetryDelay, renewalDays, takeUnseenClaudeReminders, usageFreshness, usedPercent, windowExpired } from './localClaudeReminders';

const NOW = new Date(2026, 9, 12, 12).getTime();
function usage(overrides: Partial<ClaudeUsage> = {}): ClaudeUsage {
  return { fiveHour: { usedPercent: 90, resetsAt: NOW / 1000 + 3600 }, sevenDay: null, extraUsage: null, observedAt: NOW / 1000, source: 'official_cli', error: null, ...overrides };
}
function profile(overrides: Partial<ClaudeProfile> = {}): ClaudeProfile {
  return { id: 'fixture', label: '测试账号', kind: 'managed', configDir: '/preview/cli', desktopDir: '/preview/desktop', createdAt: NOW / 1000, selected: false, collectorInstalled: false, autoRefresh: true, renewalDate: null, auth: { status: 'signed_in', email: null, plan: 'Pro', checkedAt: NOW / 1000 }, usage: usage(), ...overrides };
}

function createClaudePreviewOverview(nowMs: number): import('../types/localClaude').ClaudeOverview {
  return { profiles: [profile()], selectedProfileId: 'fixture', desktopRunning: true, codeProcessCount: 1, checkedAt: nowMs / 1000, warning: null };
}

test('zero is a known quota; absent and invalid values never turn into zero', () => {
  assert.equal(usedPercent(0), 0); assert.equal(usedPercent(null), null); assert.equal(usedPercent(undefined), null);
  assert.equal(usedPercent(NaN), null); assert.equal(usedPercent(Infinity), null); assert.equal(usedPercent(-1), null); assert.equal(usedPercent(120), 100);
});
test('freshness respects unknown, expired snapshots, future clocks and query failures', () => {
  assert.equal(usageFreshness(usage(), NOW), 'fresh');
  assert.equal(usageFreshness(usage({ observedAt: null }), NOW), 'never');
  assert.equal(usageFreshness(usage({ source: 'none' }), NOW), 'never');
  assert.equal(usageFreshness(usage({ observedAt: (NOW - CLAUDE_STALE_AFTER_MS) / 1000 }), NOW), 'stale');
  assert.equal(usageFreshness(usage({ observedAt: (NOW + 120_000) / 1000 }), NOW), 'stale');
  assert.equal(usageFreshness(usage({ error: '查询失败' }), NOW), 'stale');
});
test('reset expiry affects each window independently and never implies a new zero', () => {
  assert.equal(windowExpired({ usedPercent: 100, resetsAt: NOW / 1000 }, NOW), true);
  assert.equal(windowExpired({ usedPercent: 100, resetsAt: null }, NOW), false);
  const p = profile({ usage: usage({ fiveHour: { usedPercent: 100, resetsAt: NOW / 1000 }, sevenDay: { usedPercent: 95, resetsAt: NOW / 1000 + 86400 } }) });
  const reminders = collectClaudeReminders([p], NOW);
  assert.equal(reminders.length, 1); assert.match(reminders[0].body, /7 天/);
});
test('busy query skips preserve the freshness of a real passive sample, never its timestamp', () => {
  const sample = usage({ source: 'desktop_cache', error: 'busy', errorCode: 'busy' });
  assert.equal(usageFreshness(sample, NOW), 'fresh');
  assert.equal(usageFreshness(sample, NOW + CLAUDE_STALE_AFTER_MS), 'stale');
  assert.equal(sample.observedAt, NOW / 1000);
  assert.equal(canAutoRefreshClaude(profile({ usage: sample })), true);
  for (const errorCode of ['auth_expired', 'auth_required', 'account_disabled', 'identity_mismatch']) {
    assert.equal(canAutoRefreshClaude(profile({ usage: usage({ errorCode }) })), false);
  }
  assert.equal(canAutoRefreshClaude(profile({ usage: usage({ errorCode: 'network_unavailable' }) })), true);
});
test('collector labels distinguish installation from actually receiving a recent sample', () => {
  assert.match(collectorStatus(profile(), NOW), /未安装/);
  assert.match(collectorStatus(profile({ collectorInstalled: true }), NOW), /尚无近期/);
  const active = profile({ collectorInstalled: true, usage: usage({ source: 'official_statusline' }) });
  assert.match(collectorStatus(active, NOW), /最近收到/);
  assert.match(collectorStatus(active, NOW + CLAUDE_STALE_AFTER_MS), /尚无近期/);
});
test('official health evidence is independent of cached login and its age stays visible', () => {
  const health: import('../types/localClaude').ClaudeHealth = { status: 'available', reason: 'usage_page_available', source: 'official_ui', observedAt: NOW / 1000, lastSuccessfulAt: NOW / 1000, lastAttemptAt: null, lastAttemptOutcome: null };
  assert.match(healthLabel(profile({ health }), NOW), /未验证新模型/);
  assert.match(healthLabel(profile({ health }), NOW + CLAUDE_STALE_AFTER_MS), /已过期/);
  const disabled = profile({ health: { ...health, status: 'account_disabled', source: 'official_email' } });
  assert.equal(canAutoRefreshClaude(disabled), false);
  assert.match(healthLabel(disabled, NOW + 30 * 86400000), /官方明确停用/);
  assert.match(healthLabel(profile({ health: { ...health, status: 'auth_required' } }), NOW), /不等于封禁/);
});
test('quota thresholds are 90 and 100, not remaining percentages', () => {
  assert.equal(collectClaudeReminders([profile({ usage: usage({ fiveHour: { usedPercent: 89.9, resetsAt: NOW / 1000 + 1 } }) })], NOW).length, 0);
  assert.match(collectClaudeReminders([profile()], NOW)[0].title, /快用完/);
  assert.match(collectClaudeReminders([profile({ usage: usage({ fiveHour: { usedPercent: 100, resetsAt: NOW / 1000 + 1 } }) })], NOW)[0].title, /已达上限/);
});
test('stale, unsigned and missing-window profiles cannot generate quota alerts', () => {
  assert.equal(collectClaudeReminders([profile({ usage: usage({ error: '失败' }) })], NOW).length, 0);
  assert.equal(collectClaudeReminders([profile({ auth: { status: 'signed_out', email: null, plan: null, checkedAt: null } })], NOW).length, 0);
  assert.equal(collectClaudeReminders([profile({ usage: usage({ fiveHour: null }) })], NOW).length, 0);
});
test('repeated refresh/retry does not duplicate notifications; a new period can notify', () => {
  const shown = new Set<string>(); const first = collectClaudeReminders([profile()], NOW);
  assert.equal(takeUnseenClaudeReminders([...first, ...first], shown).length, 1);
  assert.equal(takeUnseenClaudeReminders(first, shown).length, 0);
  const next = collectClaudeReminders([profile({ usage: usage({ fiveHour: { usedPercent: 92, resetsAt: NOW / 1000 + 7200 } }) })], NOW);
  assert.equal(takeUnseenClaudeReminders(next, shown).length, 1);
});
test('a date written by the user is validated as a calendar date, not OAuth expiry', () => {
  assert.equal(renewalDays('2026-10-12', NOW), 0); assert.equal(renewalDays('2026-10-15', NOW), 3);
  assert.equal(renewalDays('2026-02-30', NOW), null); assert.equal(renewalDays('1790000000', NOW), null);
  assert.match(collectClaudeReminders([profile({ renewalDate: '2026-10-14', usage: usage({ fiveHour: null }) })], NOW)[0].body, /你记录的续费/);
});
test('extra usage converts minor units only for a known currency', () => {
  assert.match(formatMinorAmount(1250, 'USD')!, /12\.50/); assert.match(formatMinorAmount(1250, 'JPY')!, /1,250/);
  assert.equal(formatMinorAmount(1250, null), null); assert.equal(formatMinorAmount(null, 'USD'), null); assert.equal(formatMinorAmount(10, 'XYZ'), null);
});
test('refresh failures back off with a bounded delay', () => {
  assert.equal(refreshRetryDelay(1), 5000); assert.equal(refreshRetryDelay(2), 10000); assert.equal(refreshRetryDelay(100), 120000);
});



test('service maps manual and automatic refresh to the same command with distinct flags', async () => {
  const { createLocalClaudeService } = await import('../services/localClaudeService');
  const calls: { command: string; args: unknown }[] = [];
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { __TAURI_INTERNALS__: {
    invoke: async (command: string, args: unknown) => { calls.push({ command, args }); return createClaudePreviewOverview(NOW); },
  } } });
  try {
    const service = createLocalClaudeService(false);
    await service.refreshAll();
    await service.refreshAll(true);
    await service.refreshAll(false);
    assert.deepEqual(calls, [
      { command: 'local_safe_claude_refresh_all', args: { automatic: false } },
      { command: 'local_safe_claude_list', args: {} },
      { command: 'local_safe_claude_refresh_all', args: { automatic: true } },
      { command: 'local_safe_claude_list', args: {} },
      { command: 'local_safe_claude_refresh_all', args: { automatic: false } },
      { command: 'local_safe_claude_list', args: {} },
    ]);
  } finally {
    if (originalWindow) Object.defineProperty(globalThis, 'window', originalWindow);
    else Reflect.deleteProperty(globalThis, 'window');
  }
});



test('readonly browser service fetches the real local summary without credentials and rejects every native action', async () => {
  const { createLocalClaudeService } = await import('../services/localClaudeService');
  const originalFetch = globalThis.fetch;
  const requests: { url: unknown; options: RequestInit | undefined }[] = [];
  const summary = createClaudePreviewOverview(NOW);
  summary.profiles[0].label = '只读本地摘要';
  globalThis.fetch = (async (url: RequestInfo | URL, options?: RequestInit) => {
    requests.push({ url, options });
    return { ok: true, json: async () => summary } as Response;
  }) as typeof fetch;
  try {
    const service = createLocalClaudeService(true);
    assert.equal((await service.list()).profiles[0].label, '只读本地摘要');
    assert.deepEqual(requests, [{ url: '/__claude_local/overview', options: { method: 'GET', credentials: 'omit', cache: 'no-store', redirect: 'error' } }]);
    await assert.rejects(service.inspectAuth('id'), /Cockpit 应用/);
    await assert.rejects(service.create('账号'), /Cockpit 应用/);
    await assert.rejects(service.registerCurrent(), /Cockpit 应用/);
    await assert.rejects(service.select('id'), /Cockpit 应用/);
    await assert.rejects(service.refresh('id'), /Cockpit 应用/);
    await assert.rejects(service.refreshAll(), /Cockpit 应用/);
    await assert.rejects(service.preflight(), /Cockpit 应用/);
    await assert.rejects(service.action('id', 'login_cli'), /Cockpit 应用/);
    await assert.rejects(service.preferences('id', true, null), /Cockpit 应用/);
    await assert.rejects(service.notify('标题', '内容'), /Cockpit 应用/);
    await assert.rejects(service.switches(), /Cockpit 应用/);
    await assert.rejects(service.prepareSwitch('source', 'target'), /Cockpit 应用/);
    await assert.rejects(service.switchAction('operation', 'confirm', true), /Cockpit 应用/);
    await assert.rejects(service.recordHealth('profile', 'account_disabled', 'official_notice', 'official_email', NOW / 1000), /Cockpit 应用/);
    await assert.rejects(service.archiveProfile('profile', true), /Cockpit 应用/);
    await assert.rejects(service.archiveProfile('profile', false), /Cockpit 应用/);
    assert.equal(requests.length, 1, 'readonly mutations never become HTTP commands');
  } finally { globalThis.fetch = originalFetch; }
});

test('native switch and health records send only explicit structured actions', async () => {
  const { createLocalClaudeService } = await import('../services/localClaudeService');
  const calls: { command: string; args: unknown }[] = [];
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { __TAURI_INTERNALS__: {
    invoke: async (command: string, args: unknown) => { calls.push({ command, args }); return createClaudePreviewOverview(NOW); },
  } } });
  try {
    const service = createLocalClaudeService();
    await service.prepareSwitch('source', 'target');
    await service.switchAction('operation', 'request_quit', true);
    await service.switchAction('operation', 'advance');
    await service.recordHealth('target', 'account_disabled', 'supported_countries_policy', 'official_email', NOW / 1000);
    await service.archiveProfile('target', true);
    await service.archiveProfile('target', false);
    assert.deepEqual(calls, [
      { command: 'local_safe_claude_switch', args: { action: 'prepare', sourceId: 'source', targetId: 'target' } },
      { command: 'local_safe_claude_switch', args: { action: 'request_quit', operationId: 'operation', confirmed: true } },
      { command: 'local_safe_claude_switch', args: { action: 'advance', operationId: 'operation', confirmed: false } },
      { command: 'local_safe_claude_record_health', args: { profileId: 'target', status: 'account_disabled', reason: 'supported_countries_policy', source: 'official_email', observedAt: NOW / 1000 } },
      { command: 'local_safe_claude_list', args: {} },
      { command: 'local_safe_claude_archive_profile', args: { profileId: 'target', archived: true } },
      { command: 'local_safe_claude_archive_profile', args: { profileId: 'target', archived: false } },
    ]);
  } finally {
    if (originalWindow) Object.defineProperty(globalThis, 'window', originalWindow);
    else Reflect.deleteProperty(globalThis, 'window');
  }
});

test('native identity inspection requests metadata projection, never login', async () => {
  const { createLocalClaudeService } = await import('../services/localClaudeService');
  const calls: { command: string; args: unknown }[] = [];
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { __TAURI_INTERNALS__: {
    invoke: async (command: string, args: unknown) => { calls.push({ command, args }); return createClaudePreviewOverview(NOW); },
  } } });
  try {
    await createLocalClaudeService().inspectAuth('registered-profile');
    assert.deepEqual(calls, [{ command: 'local_safe_claude_inspect_auth', args: { profileId: 'registered-profile' } }, { command: 'local_safe_claude_list', args: {} }]);
  } finally {
    if (originalWindow) Object.defineProperty(globalThis, 'window', originalWindow);
    else Reflect.deleteProperty(globalThis, 'window');
  }
});

test('changing the selected account keeps newer passive samples and the operation warning', async () => {
  const { createLocalClaudeService } = await import('../services/localClaudeService');
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { __TAURI_INTERNALS__: {
    invoke: async (command: string) => {
      const result = createClaudePreviewOverview(NOW);
      result.profiles[0].usage.observedAt = command === 'local_safe_claude_list' ? NOW / 1000 : 1;
      result.warning = command === 'local_safe_claude_select' ? 'stored operation warning' : null;
      return result;
    },
  } } });
  try {
    const result = await createLocalClaudeService().select('fixture');
    assert.equal(result.profiles[0].usage.observedAt, NOW / 1000);
    assert.equal(result.warning, 'stored operation warning');
  } finally {
    if (originalWindow) Object.defineProperty(globalThis, 'window', originalWindow);
    else Reflect.deleteProperty(globalThis, 'window');
  }
});


test('Desktop cache retains its original age and does not turn metadata into verified login', () => {
  const cached = usage({ source: 'desktop_cache', observedAt: (NOW - CLAUDE_STALE_AFTER_MS - 1) / 1000 });
  assert.equal(usageFreshness(cached, NOW), 'stale');
  const metadata = profile({ auth: { status: 'unknown', email: 'fixture@example.invalid', plan: 'Pro', checkedAt: NOW / 1000, source: 'cached_metadata' }, usage: usage({ source: 'desktop_cache' }) });
  assert.equal(collectClaudeReminders([metadata], NOW).length, 0);
  assert.equal(metadata.auth.email, 'fixture@example.invalid');
});

test('session continuation requires known idle source and target, including the same profile', async () => {
  const { sessionResumeBlockReason } = await import('./localClaudeReminders');
  const session: import('../types/localClaude').ClaudeSession = { sessionId: 'safe-id', label: '项目会话', cwd: '/test/project', projectKey: 'project', updatedAt: NOW / 1000, active: false, activity: 'idle', parseStatus: 'ok', stable: true, files: 2, bytes: 100 };
  const inventory: import('../types/localClaude').ClaudeSessionInventory = { checkedAt: NOW / 1000, profileId: 'fixture', active: false, activityKnown: true, sessions: [session], summary: { files: 2, bytes: 100, jsonlFiles: 2, incompleteFiles: 0, invalidFiles: 0, unstableFiles: 0, unverifiedFiles: 0, symlinksSkipped: 0 } };
  assert.equal(sessionResumeBlockReason(inventory, inventory, session), null);
  assert.match(sessionResumeBlockReason({ ...inventory, active: true }, inventory, session)!, /源配置/);
  assert.match(sessionResumeBlockReason(inventory, { ...inventory, active: true }, session)!, /所选账号/);
  assert.match(sessionResumeBlockReason(inventory, { ...inventory, activityKnown: false }, session)!, /尚未确认/);
  assert.match(sessionResumeBlockReason(inventory, null, session)!, /尚未确认/);
  assert.match(sessionResumeBlockReason(inventory, inventory, { ...session, parseStatus: 'incomplete' })!, /记录需要检查/);
  assert.match(sessionResumeBlockReason(inventory, inventory, { ...session, stable: false })!, /尚未稳定/);
});

test('session read, snapshot and resume map to dedicated native commands', async () => {
  const { createLocalClaudeService } = await import('../services/localClaudeService');
  const calls: { command: string; args: unknown }[] = [];
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { __TAURI_INTERNALS__: {
    invoke: async (command: string, args: unknown) => { calls.push({ command, args }); return {}; },
  } } });
  try {
    const service = createLocalClaudeService();
    await service.sessions('source'); await service.snapshot('source'); await service.resume('source', 'target', 'session');
    assert.deepEqual(calls, [
      { command: 'local_safe_claude_sessions', args: { profileId: 'source' } },
      { command: 'local_safe_claude_snapshot', args: { profileId: 'source' } },
      { command: 'local_safe_claude_resume', args: { sourceProfileId: 'source', targetProfileId: 'target', sessionId: 'session' } },
    ]);
  } finally {
    if (originalWindow) Object.defineProperty(globalThis, 'window', originalWindow);
    else Reflect.deleteProperty(globalThis, 'window');
  }
});

test('browser session reading is local GET only; preservation and resume cannot be posted', async () => {
  const { createLocalClaudeService } = await import('../services/localClaudeService');
  const originalFetch = globalThis.fetch;
  const requests: { url: unknown; options: RequestInit | undefined }[] = [];
  globalThis.fetch = (async (url: RequestInfo | URL, options?: RequestInit) => { requests.push({ url, options }); return { ok: true, json: async () => ({ sessions: [] }) } as Response; }) as typeof fetch;
  try {
    const service = createLocalClaudeService(true);
    await service.sessions('profile/id');
    await assert.rejects(service.snapshot('profile'), /Cockpit 应用/);
    await assert.rejects(service.resume('profile', 'other', 'session'), /Cockpit 应用/);
    assert.deepEqual(requests, [{ url: '/__claude_local/sessions?profileId=profile%2Fid', options: { method: 'GET', credentials: 'omit', cache: 'no-store', redirect: 'error' } }]);
  } finally { globalThis.fetch = originalFetch; }
});


test('process activity labels never claim that a model or tool is executing', async () => {
  const { sessionActivityLabel } = await import('./localClaudeReminders');
  assert.equal(sessionActivityLabel('active'), '会话进程仍在');
  assert.equal(sessionActivityLabel('possibly_active'), '配置正在使用');
  assert.equal(sessionActivityLabel('unknown'), '状态待确认');
  assert.equal(sessionActivityLabel('idle'), '未检测到会话进程');
});
