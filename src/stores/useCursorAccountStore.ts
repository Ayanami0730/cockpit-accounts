import {
  CursorAccount,
  getCursorAccountDisplayEmail,
  getCursorPlanBadge,
  getCursorUsage,
} from '../types/cursor';
import * as cursorService from '../services/cursorService';
import { createProviderAccountStore } from './createProviderAccountStore';

const CURSOR_ACCOUNTS_CACHE_KEY = 'agtools.cursor.accounts.cache';
const CURSOR_CURRENT_ACCOUNT_ID_KEY = 'agtools.cursor.current_account_id';

export const useCursorAccountStore = createProviderAccountStore<CursorAccount>(
  CURSOR_ACCOUNTS_CACHE_KEY,
  {
    listAccounts: cursorService.listCursorAccounts,
    deleteAccount: cursorService.deleteCursorAccount,
    deleteAccounts: cursorService.deleteCursorAccounts,
    injectAccount: cursorService.injectCursorAccount,
    refreshToken: cursorService.refreshCursorToken,
    refreshAllTokens: cursorService.refreshAllCursorTokens,
    importFromJson: cursorService.importCursorFromJson,
    exportAccounts: cursorService.exportCursorAccounts,
    updateAccountTags: cursorService.updateCursorAccountTags,
  },
  {
    getDisplayEmail: getCursorAccountDisplayEmail,
    getPlanBadge: getCursorPlanBadge,
    getUsage: getCursorUsage,
  },
  {
    platformId: 'cursor',
    currentAccountIdKey: CURSOR_CURRENT_ACCOUNT_ID_KEY,
    // The local build remembers only its own last manual selection. Resolving
    // the official client's current account would read its auth on startup.
  },
);
