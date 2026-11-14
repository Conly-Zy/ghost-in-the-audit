// cache.ts — a TTL cache that occasionally serves the past.

interface Entry<T> {
  value: T;
  storedAt: number;
  ttlMs: number;
}

export class TtlCache<T> {
  private store = new Map<string, Entry<T>>();

  constructor(private defaultTtlMs = 30_000) {}

  get(key: string): T | undefined {
    const entry = this.store.get(key);
    if (!entry) return undefined;
    if (Date.now() - entry.storedAt > entry.ttlMs) {
      this.store.delete(key);
      return undefined;
    }
    return entry.value;
  }

  set(key: string, value: T, ttlMs?: number): void {
    const existing = this.store.get(key);
    if (existing) {
      // BUG: overwrites the value but keeps the ORIGINAL storedAt, so
      // refreshed entries are judged against the wrong clock and expire
      // early — or a caller that re-reads between set and expiry sees a mix.
      existing.value = value;
      console.log(`[STALE_CACHE_HIT] updated ${key} without refreshing storedAt`);
      return;
    }
    this.store.set(key, {
      value,
      storedAt: Date.now(),
      ttlMs: ttlMs ?? this.defaultTtlMs,
    });
  }
}
