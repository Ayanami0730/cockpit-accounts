import type { ClaudeAction, ClaudeActionResult, ClaudeOverview, ClaudePreflight, ClaudeSessionInventory, ClaudeSessionSnapshot, ClaudeHealth, ClaudeSwitchOperation, ClaudeSwitchAction } from '../types/localClaude';

export interface LocalClaudeService {
  list(): Promise<ClaudeOverview>;
  inspectAuth(profileId: string): Promise<ClaudeOverview>;
  create(label: string): Promise<ClaudeOverview>;
  registerCurrent(): Promise<ClaudeOverview>;
  select(profileId: string): Promise<ClaudeOverview>;
  refresh(profileId: string): Promise<ClaudeOverview>;
  refreshAll(automatic?: boolean): Promise<ClaudeOverview>;
  preflight(): Promise<ClaudePreflight>;
  action(profileId: string, action: ClaudeAction): Promise<ClaudeActionResult>;
  preferences(profileId: string, autoRefresh: boolean, renewalDate: string | null): Promise<ClaudeOverview>;
  notify(title: string, body: string): Promise<void>;
  sessions(profileId: string): Promise<ClaudeSessionInventory>;
  snapshot(profileId: string): Promise<ClaudeSessionSnapshot>;
  resume(sourceProfileId: string, targetProfileId: string, sessionId: string): Promise<ClaudeActionResult>;
  switches(): Promise<ClaudeSwitchOperation[]>;
  prepareSwitch(sourceId: string, targetId: string): Promise<ClaudeSwitchOperation>;
  switchAction(operationId: string, action: ClaudeSwitchAction, confirmed?: boolean): Promise<ClaudeSwitchOperation>;
  recordHealth(profileId: string, status: ClaudeHealth['status'], reason: string, source: ClaudeHealth['source'], observedAt: number): Promise<ClaudeOverview>;
  archiveProfile(profileId: string, archived: boolean): Promise<ClaudeActionResult>;
}

async function readLocalJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { method: 'GET', credentials: 'omit', cache: 'no-store', redirect: 'error' });
  if (!response.ok) throw new Error('本地只读摘要暂不可用');
  return response.json() as Promise<T>;
}

/** Browser mode can only read the local mirror; it has no command endpoint or synthetic fallback. */
function createReadonlyService(): LocalClaudeService {
  const inApp = async (): Promise<never> => { throw new Error('请在 Cockpit 应用中操作'); };
  return {
    list: () => readLocalJson<ClaudeOverview>('/__claude_local/overview'),
    sessions: (profileId) => readLocalJson<ClaudeSessionInventory>(`/__claude_local/sessions?profileId=${encodeURIComponent(profileId)}`),
    inspectAuth: inApp, create: inApp, registerCurrent: inApp, select: inApp, refresh: inApp,
    refreshAll: inApp, preflight: inApp, action: inApp, preferences: inApp, notify: inApp, snapshot: inApp, resume: inApp,
    switches: inApp, prepareSwitch: inApp, switchAction: inApp, recordHealth: inApp, archiveProfile: inApp,
  };
}

export function createLocalClaudeService(readonly = false): LocalClaudeService {
  if (readonly) return createReadonlyService();
  const call = async <T,>(command: string, args?: Record<string, unknown>): Promise<T> => {
    const { invoke } = await import('@tauri-apps/api/core');
    return invoke<T>(command, args);
  };
  const list = () => call<ClaudeOverview>('local_safe_claude_list');
  // Mutations still return saved snapshots. Re-project passive data so changing
  // a preference cannot replace a newer Desktop sample with an older one.
  const withPassiveOverview = async (command: string, args?: Record<string, unknown>) => {
    const result = await call<ClaudeOverview>(command, args);
    const current = await list();
    return { ...current, warning: result.warning ?? current.warning };
  };
  return {
    list,
    inspectAuth: (profileId) => withPassiveOverview('local_safe_claude_inspect_auth', { profileId }),
    create: (label) => withPassiveOverview('local_safe_claude_create', { label }),
    registerCurrent: () => withPassiveOverview('local_safe_claude_register_current'),
    select: (profileId) => withPassiveOverview('local_safe_claude_select', { profileId }),
    refresh: (profileId) => withPassiveOverview('local_safe_claude_refresh', { profileId }),
    refreshAll: (automatic = false) => withPassiveOverview('local_safe_claude_refresh_all', { automatic }),
    preflight: () => call('local_safe_claude_preflight'),
    action: (profileId, action) => call('local_safe_claude_action', { profileId, action }),
    preferences: (profileId, autoRefresh, renewalDate) => withPassiveOverview('local_safe_claude_preferences', { profileId, autoRefresh, renewalDate }),
    notify: (title, body) => call('local_safe_claude_notify', { title, body }),
    sessions: (profileId) => call('local_safe_claude_sessions', { profileId }),
    snapshot: (profileId) => call('local_safe_claude_snapshot', { profileId }),
    resume: (sourceProfileId, targetProfileId, sessionId) => call('local_safe_claude_resume', { sourceProfileId, targetProfileId, sessionId }),
    switches: () => call('local_safe_claude_switch', { action: 'list' }),
    prepareSwitch: (sourceId, targetId) => call('local_safe_claude_switch', { action: 'prepare', sourceId, targetId }),
    switchAction: (operationId, action, confirmed = false) => call('local_safe_claude_switch', { action, operationId, confirmed }),
    recordHealth: (profileId, status, reason, source, observedAt) => withPassiveOverview('local_safe_claude_record_health', { profileId, status, reason, source, observedAt }),
    archiveProfile: (profileId, archived) => call('local_safe_claude_archive_profile', { profileId, archived }),
  };
}
