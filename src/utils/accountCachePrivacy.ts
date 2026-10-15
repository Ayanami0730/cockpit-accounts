/** Remove only this app's legacy account caches, never inspect their contents. */
export function removeLegacyAccountCache(...keys: string[]): void {
  for (const key of keys) {
    try {
      localStorage.removeItem(key);
    } catch {
      // Storage may be unavailable. Account stores still remain memory-only.
    }
  }
}

export function purgeLegacyAccountCaches(): void {
  try {
    const keys: string[] = [];
    for (let index = 0; index < localStorage.length; index += 1) {
      const key = localStorage.key(index);
      if (key && /^agtools\.(?:[\w-]+\.)?accounts\.(?:cache|current|store)(?:\.|$)/.test(key)) {
        keys.push(key);
      }
    }
    removeLegacyAccountCache(...keys);
  } catch {
    // Do not make startup depend on optional WebView storage.
  }
}
