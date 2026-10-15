import { create } from 'zustand';
import { removeLegacyAccountCache } from '../utils/accountCachePrivacy';
import { Account, AccountNoteUpdate, RefreshStats } from '../types/account';
import * as accountService from '../services/accountService';
import { emitAccountsChanged, emitCurrentAccountChanged } from '../utils/accountSyncEvents';
import {
  AntigravityRuntimeTarget,
  DEFAULT_ANTIGRAVITY_RUNTIME_TARGET,
  normalizeAntigravityRuntimeTarget,
} from '../utils/antigravityRuntimeTarget';

const ACCOUNTS_STORE_KEY = 'agtools.accounts.store.v1';
const LEGACY_ACCOUNTS_CACHE_KEY = 'agtools.accounts.cache';
const LEGACY_CURRENT_ACCOUNT_CACHE_KEY = 'agtools.accounts.current';

// Older snapshots used a denylist that could retain new credential fields.
// This store is now memory-only, including its current-account snapshots.
removeLegacyAccountCache(
  ACCOUNTS_STORE_KEY,
  LEGACY_ACCOUNTS_CACHE_KEY,
  LEGACY_CURRENT_ACCOUNT_CACHE_KEY,
);

// 防抖状态（在 store 外部维护，避免触发 re-render）
let fetchAccountsPromise: Promise<void> | null = null;
let fetchAccountsLastTime = 0;
let fetchAccountsSeq = 0;
const fetchCurrentPromises: Partial<Record<AntigravityRuntimeTarget, Promise<void>>> = {};
const fetchCurrentLastTimes: Partial<Record<AntigravityRuntimeTarget, number>> = {};
let allowNextEmptyAccountList = false;
const allowNextEmptyCurrentAccountByTarget: Partial<Record<AntigravityRuntimeTarget, boolean>> = {};
const DEBOUNCE_MS = 500;

type CurrentAccountsByTarget = Record<AntigravityRuntimeTarget, Account | null>;

function buildEmptyCurrentAccountsByTarget(): CurrentAccountsByTarget {
    return {
        antigravity: null,
        antigravity_ide: null,
    };
}

function resolveRuntimeTarget(runtimeTarget?: AntigravityRuntimeTarget): AntigravityRuntimeTarget {
    return normalizeAntigravityRuntimeTarget(runtimeTarget ?? DEFAULT_ANTIGRAVITY_RUNTIME_TARGET);
}

function updateCurrentAccountsByTarget(
    current: CurrentAccountsByTarget | undefined,
    runtimeTarget: AntigravityRuntimeTarget,
    account: Account | null,
): CurrentAccountsByTarget {
    return {
        ...buildEmptyCurrentAccountsByTarget(),
        ...(current ?? {}),
        [runtimeTarget]: account,
    };
}

interface AccountState {
    accounts: Account[];
    currentAccount: Account | null;
    currentAccountsByTarget: CurrentAccountsByTarget;
    loading: boolean;
    error: string | null;
    fetchAccounts: () => Promise<void>;
    fetchCurrentAccount: (runtimeTarget?: AntigravityRuntimeTarget) => Promise<void>;
    addAccount: (email: string, refreshToken: string) => Promise<Account>;
    deleteAccount: (accountId: string) => Promise<void>;
    deleteAccounts: (accountIds: string[]) => Promise<void>;
    setCurrentAccount: (accountId: string, runtimeTarget?: AntigravityRuntimeTarget) => Promise<void>;
    refreshQuota: (accountId: string, runtimeTarget?: AntigravityRuntimeTarget) => Promise<void>;
    refreshAllQuotas: (trigger?: 'auto' | 'manual') => Promise<RefreshStats>;
    startOAuthLogin: (update?: AccountNoteUpdate) => Promise<Account>;
    reorderAccounts: (accountIds: string[]) => Promise<void>;
    switchAccount: (accountId: string, runtimeTarget?: AntigravityRuntimeTarget) => Promise<Account>;
    syncCurrentFromClient: () => Promise<void>;
    updateAccountTags: (accountId: string, tags: string[]) => Promise<Account>;
    updateAccountNotes: (accountId: string, update: string | AccountNoteUpdate) => Promise<Account>;
}

export const useAccountStore = create<AccountState>()(
    (set, get) => ({
      accounts: [],
      currentAccount: null,
      currentAccountsByTarget: buildEmptyCurrentAccountsByTarget(),
      loading: false,
      error: null,

      fetchAccounts: async () => {
          const now = Date.now();
          
          // 如果正在请求中，且距离上次请求不足 DEBOUNCE_MS，复用现有 Promise
          if (fetchAccountsPromise && now - fetchAccountsLastTime < DEBOUNCE_MS) {
              return fetchAccountsPromise;
          }
          
          fetchAccountsLastTime = now;
          
          fetchAccountsPromise = (async () => {
              const requestId = ++fetchAccountsSeq;
              set({ loading: true, error: null });
              try {
                  const accounts = await accountService.listAccounts();
                  if (requestId !== fetchAccountsSeq) {
                      return;
                  }
                  if (accounts.length === 0 && get().accounts.length > 0 && !allowNextEmptyAccountList) {
                      console.warn('[AccountStore] 忽略异常空账号列表，保留本地缓存账号');
                      set({ loading: false });
                      return;
                  }
                  allowNextEmptyAccountList = false;
                  set({ accounts, loading: false });
              } catch (e) {
                  if (requestId !== fetchAccountsSeq) {
                      return;
                  }
                  set({ error: String(e), loading: false });
              } finally {
                  if (requestId === fetchAccountsSeq) {
                      allowNextEmptyAccountList = false;
                  }
                  // 请求完成后延迟清除 Promise，允许短时间内的后续调用也复用结果
                  setTimeout(() => {
                      if (requestId === fetchAccountsSeq) {
                          fetchAccountsPromise = null;
                      }
                  }, 100);
              }
          })();
          
          return fetchAccountsPromise;
      },

      fetchCurrentAccount: async (runtimeTarget) => {
          const target = resolveRuntimeTarget(runtimeTarget);
          const now = Date.now();
          
          // 防抖：复用正在进行的请求
          if (
              fetchCurrentPromises[target] &&
              now - (fetchCurrentLastTimes[target] ?? 0) < DEBOUNCE_MS
          ) {
              return fetchCurrentPromises[target];
          }
          
          fetchCurrentLastTimes[target] = now;
          
          fetchCurrentPromises[target] = (async () => {
              try {
                  const account = await accountService.getCurrentAccount(target);
                  if (
                      !account &&
                      get().currentAccountsByTarget[target] &&
                      get().accounts.length > 0 &&
                      !allowNextEmptyCurrentAccountByTarget[target]
                  ) {
                      console.warn(`[AccountStore] 忽略异常空当前账号，保留本地缓存当前账号: ${target}`);
                      return;
                  }
                  allowNextEmptyCurrentAccountByTarget[target] = false;
                  set((state) => ({
                      currentAccount: account,
                      currentAccountsByTarget: updateCurrentAccountsByTarget(
                          state.currentAccountsByTarget,
                          target,
                          account,
                      ),
                  }));
              } catch (e) {
                  console.error('Failed to fetch current account:', e);
              } finally {
                  allowNextEmptyCurrentAccountByTarget[target] = false;
                  setTimeout(() => {
                      fetchCurrentPromises[target] = undefined;
                  }, 100);
              }
          })();
          
          return fetchCurrentPromises[target];
      },

    addAccount: async (email: string, refreshToken: string) => {
        const account = await accountService.addAccount(email, refreshToken);
        await get().fetchAccounts();
        await emitAccountsChanged({
            platformId: 'antigravity',
            reason: 'import',
        });
        return account;
    },

    deleteAccount: async (accountId: string) => {
        const previousCurrentAccounts = get().currentAccountsByTarget;
        allowNextEmptyAccountList = get().accounts.length <= 1;
        allowNextEmptyCurrentAccountByTarget.antigravity =
            previousCurrentAccounts.antigravity?.id === accountId;
        allowNextEmptyCurrentAccountByTarget.antigravity_ide =
            previousCurrentAccounts.antigravity_ide?.id === accountId;
        try {
            await accountService.deleteAccount(accountId);
            await get().fetchAccounts();
            await Promise.allSettled([
                get().fetchCurrentAccount('antigravity'),
                get().fetchCurrentAccount('antigravity_ide'),
            ]);
        } finally {
            allowNextEmptyAccountList = false;
            allowNextEmptyCurrentAccountByTarget.antigravity = false;
            allowNextEmptyCurrentAccountByTarget.antigravity_ide = false;
        }
        await emitAccountsChanged({
            platformId: 'antigravity',
            reason: 'delete',
        });
        for (const target of ['antigravity', 'antigravity_ide'] as const) {
            const previousCurrentAccountId = previousCurrentAccounts[target]?.id ?? null;
            const nextCurrentAccountId = get().currentAccountsByTarget[target]?.id ?? null;
            if (previousCurrentAccountId !== nextCurrentAccountId) {
                await emitCurrentAccountChanged({
                    platformId: target,
                    accountId: nextCurrentAccountId,
                    reason: 'delete',
                });
            }
        }
    },

    deleteAccounts: async (accountIds: string[]) => {
        const previousCurrentAccounts = get().currentAccountsByTarget;
        const deleteIdSet = new Set(accountIds);
        allowNextEmptyAccountList = get().accounts.every((account) => deleteIdSet.has(account.id));
        allowNextEmptyCurrentAccountByTarget.antigravity =
            previousCurrentAccounts.antigravity?.id
                ? deleteIdSet.has(previousCurrentAccounts.antigravity.id)
                : false;
        allowNextEmptyCurrentAccountByTarget.antigravity_ide =
            previousCurrentAccounts.antigravity_ide?.id
                ? deleteIdSet.has(previousCurrentAccounts.antigravity_ide.id)
                : false;
        try {
            await accountService.deleteAccounts(accountIds);
            await get().fetchAccounts();
            await Promise.allSettled([
                get().fetchCurrentAccount('antigravity'),
                get().fetchCurrentAccount('antigravity_ide'),
            ]);
        } finally {
            allowNextEmptyAccountList = false;
            allowNextEmptyCurrentAccountByTarget.antigravity = false;
            allowNextEmptyCurrentAccountByTarget.antigravity_ide = false;
        }
        await emitAccountsChanged({
            platformId: 'antigravity',
            reason: 'delete',
        });
        for (const target of ['antigravity', 'antigravity_ide'] as const) {
            const previousCurrentAccountId = previousCurrentAccounts[target]?.id ?? null;
            const nextCurrentAccountId = get().currentAccountsByTarget[target]?.id ?? null;
            if (previousCurrentAccountId !== nextCurrentAccountId) {
                await emitCurrentAccountChanged({
                    platformId: target,
                    accountId: nextCurrentAccountId,
                    reason: 'delete',
                });
            }
        }
    },

    setCurrentAccount: async (accountId: string, runtimeTarget) => {
        const target = resolveRuntimeTarget(runtimeTarget);
        await accountService.setCurrentAccount(accountId, target);
        await get().fetchCurrentAccount(target);
        await emitCurrentAccountChanged({
            platformId: target,
            accountId: get().currentAccountsByTarget[target]?.id ?? accountId,
            reason: 'switch',
        });
    },

    refreshQuota: async (accountId: string, runtimeTarget) => {
        const target = resolveRuntimeTarget(runtimeTarget);
        try {
            const updatedAccount = await accountService.fetchAccountQuota(accountId);
            // 成功：后端已更新该账号并返回最新状态（包含 quota_error），局部更新该账号，保持滚动位置不变
            set((state) => ({
                accounts: state.accounts.map((acc) =>
                    acc.id === accountId ? updatedAccount : acc
                ),
            }));
            
            set((state) => {
                let nextByTarget = state.currentAccountsByTarget;
                for (const currentTarget of ['antigravity', 'antigravity_ide'] as const) {
                    if (nextByTarget[currentTarget]?.id === accountId) {
                        nextByTarget = updateCurrentAccountsByTarget(
                            nextByTarget,
                            currentTarget,
                            updatedAccount,
                        );
                    }
                }
                return {
                    currentAccount:
                        nextByTarget[target]?.id === accountId
                            ? updatedAccount
                            : state.currentAccount?.id === accountId
                              ? updatedAccount
                              : state.currentAccount,
                    currentAccountsByTarget: nextByTarget,
                };
            });

            // 如果后端返回了配额错误信息，需要抛出异常让 UI 捕获并显示为失败（红叉）
            if (updatedAccount.quota_error) {
                throw new Error(updatedAccount.quota_error.message);
            }
            if (updatedAccount.quota?.is_forbidden) {
                throw new Error("403 Forbidden");
            }
        } catch (e) {
            // Token 级别失败（如 invalid_grant 会改变 disabled 状态）：全量刷新确保数据正确
            // 如果是我们自己 throw 的配额错误，因为状态已经局部更新，不再需要全量刷新
            const isQuotaError = e instanceof Error && (
                get().accounts.find(a => a.id === accountId)?.quota_error?.message === e.message ||
                e.message === "403 Forbidden"
            );
            if (!isQuotaError) {
                await get().fetchAccounts();
            }
            throw e;
        } finally {
            await get().fetchCurrentAccount(target);
        }
    },

    refreshAllQuotas: async (trigger) => {
        const stats = await accountService.refreshAllQuotas(trigger);
        await get().fetchAccounts();
        await Promise.allSettled([
            get().fetchCurrentAccount('antigravity'),
            get().fetchCurrentAccount('antigravity_ide'),
        ]);
        return stats;
    },

    startOAuthLogin: async (update?: AccountNoteUpdate) => {
        const account = await accountService.startOAuthLogin(update);
        await get().fetchAccounts();
        await emitAccountsChanged({
            platformId: 'antigravity',
            reason: 'oauth',
        });
        return account;
    },

    reorderAccounts: async (accountIds: string[]) => {
        await accountService.reorderAccounts(accountIds);
        await get().fetchAccounts();
    },

    switchAccount: async (accountId: string, runtimeTarget?: AntigravityRuntimeTarget) => {
        const target = resolveRuntimeTarget(runtimeTarget);
        const previousCurrentAccountId = get().currentAccountsByTarget[target]?.id ?? null;
        try {
            const account = await accountService.switchAccount(accountId, target);
            set((state) => ({
                currentAccount: account,
                currentAccountsByTarget: updateCurrentAccountsByTarget(
                    state.currentAccountsByTarget,
                    target,
                    account,
                ),
            }));
            await get().fetchAccounts();
            await emitCurrentAccountChanged({
                platformId: target,
                accountId: account.id,
                reason: 'switch',
            });
            return account;
        } catch (error) {
            await get().fetchAccounts();
            await get().fetchCurrentAccount(target);
            const nextCurrentAccountId = get().currentAccountsByTarget[target]?.id ?? null;
            if (previousCurrentAccountId !== nextCurrentAccountId) {
                await emitCurrentAccountChanged({
                    platformId: target,
                    accountId: nextCurrentAccountId,
                    reason: 'switch',
                });
            }
            throw error;
        }
    },

    syncCurrentFromClient: async () => {
        const target = DEFAULT_ANTIGRAVITY_RUNTIME_TARGET;
        const previousCurrentAccountId = get().currentAccountsByTarget[target]?.id ?? null;
        try {
            const account = await accountService.getCurrentAccount(target);
            set((state) => ({
                currentAccount: account,
                currentAccountsByTarget: updateCurrentAccountsByTarget(
                    state.currentAccountsByTarget,
                    target,
                    account,
                ),
            }));
            const nextCurrentAccountId = account?.id ?? null;
            if (previousCurrentAccountId !== nextCurrentAccountId) {
                await emitCurrentAccountChanged({
                    platformId: target,
                    accountId: nextCurrentAccountId,
                    reason: 'sync',
                });
            }
        } catch (e) {
            console.error('Failed to refresh current account:', e);
        }
    },

    updateAccountTags: async (accountId: string, tags: string[]) => {
        const account = await accountService.updateAccountTags(accountId, tags);
        await get().fetchAccounts();
        return account;
    },

    updateAccountNotes: async (accountId: string, update: string | AccountNoteUpdate) => {
        const account = typeof update === 'string'
            ? await accountService.updateAccountNotes(accountId, update)
            : await accountService.updateAccountNote(accountId, update);
        set((state) => ({
            accounts: state.accounts.map((item) => item.id === account.id ? account : item),
            currentAccount: state.currentAccount?.id === account.id ? account : state.currentAccount,
            currentAccountsByTarget: {
                antigravity:
                    state.currentAccountsByTarget.antigravity?.id === account.id
                        ? account
                        : state.currentAccountsByTarget.antigravity,
                antigravity_ide:
                    state.currentAccountsByTarget.antigravity_ide?.id === account.id
                        ? account
                        : state.currentAccountsByTarget.antigravity_ide,
            },
        }));
        await emitAccountsChanged({ platformId: 'antigravity', accountId: account.id, reason: 'note' });
        return account;
    },
  })
);
