import assert from 'node:assert/strict';
import test from 'node:test';
import { purgeLegacyAccountCaches } from './accountCachePrivacy';
import { createProviderAccountStore } from '../stores/createProviderAccountStore';

class MemoryStorage implements Storage {
  values = new Map<string, string>();
  reads: string[] = [];
  writes: string[] = [];
  get length() { return this.values.size; }
  clear() { this.values.clear(); }
  getItem(key: string) { this.reads.push(key); return this.values.get(key) ?? null; }
  key(index: number) { return [...this.values.keys()][index] ?? null; }
  removeItem(key: string) { this.values.delete(key); }
  setItem(key: string, value: string) { this.writes.push(key); this.values.set(key, value); }
}

test('legacy credential caches are removed without reading them or deleting UI preferences', () => {
  const storage = new MemoryStorage();
  Object.defineProperty(globalThis, 'localStorage', { value: storage, configurable: true });
  for (const key of ['agtools.cursor.accounts.cache', 'agtools.codex.accounts.current.dev', 'agtools.claude.accounts.cache.v2', 'agtools.accounts.store.v1']) {
    storage.values.set(key, 'old credential snapshot');
  }
  storage.values.set('agtools.cursor.current_account_id', 'account-1');
  storage.values.set('agtools.platform_layout.v1', 'user preference');
  purgeLegacyAccountCaches();
  assert.deepEqual([...storage.values.keys()], ['agtools.cursor.current_account_id', 'agtools.platform_layout.v1']);
  assert.deepEqual(storage.reads, []);
  assert.deepEqual(storage.writes, []);
});

test('provider accounts remain usable in memory without writing credentials to browser storage', async () => {
  const storage = new MemoryStorage();
  Object.defineProperty(globalThis, 'localStorage', { value: storage, configurable: true });
  const cacheKey = 'agtools.cursor.accounts.cache';
  storage.values.set(cacheKey, JSON.stringify([{ id: 'old', access_token: 'stale-secret' }]));
  const account = { id: 'new', email: 'fixture@example.invalid', access_token: 'fixture-secret', credentials: { refresh_token: 'nested-secret' } };
  const store = createProviderAccountStore(cacheKey, {
    listAccounts: async () => [account],
    deleteAccount: async () => {},
    deleteAccounts: async () => {},
    injectAccount: async () => {},
    refreshToken: async () => {},
    refreshAllTokens: async () => {},
    importFromJson: async () => [account],
    exportAccounts: async () => '',
    updateAccountTags: async () => account,
  }, {
    getDisplayEmail: (item) => item.email,
    getPlanBadge: () => 'PRO',
    getUsage: () => ({ inlineSuggestionsUsedPercent: 25, chatMessagesUsedPercent: 50 }),
  }, { platformId: 'cursor' });

  assert.deepEqual(store.getState().accounts, []);
  assert.equal(storage.values.has(cacheKey), false);
  await store.getState().fetchAccounts();
  const loaded = store.getState().accounts[0];
  assert.equal(loaded.email, account.email);
  assert.equal(loaded.access_token, account.access_token);
  assert.deepEqual(loaded.credentials, account.credentials);
  assert.equal(store.getState().loading, false);
  assert.equal(store.getState().error, null);
  assert.deepEqual(storage.reads, []);
  assert.deepEqual(storage.writes, []);
});

test('unavailable browser storage does not prevent account store creation', () => {
  Object.defineProperty(globalThis, 'localStorage', { get: () => { throw new Error('Storage unavailable'); }, configurable: true });
  assert.doesNotThrow(purgeLegacyAccountCaches);
  delete (globalThis as { localStorage?: Storage }).localStorage;
});
