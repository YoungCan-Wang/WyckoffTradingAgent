import { readFreshMarketWatchSnapshot, type MarketWatchSnapshot } from '@wyckoff/shared'

const MARKET_WATCH_STORAGE_VERSION = 'reading-room-market-watch-v1'

export function readMarketWatchCache(userId: string | undefined, codes: string[]): MarketWatchSnapshot | null {
  if (typeof window === 'undefined' || codes.length === 0) return null
  try {
    const key = storageKey(userId)
    const raw = window.localStorage.getItem(key)
    if (!raw) return null
    const value = JSON.parse(raw) as unknown
    const snapshot = readFreshMarketWatchSnapshot(value, codes)
    if (!snapshot) window.localStorage.removeItem(key)
    return snapshot
  } catch {
    try { window.localStorage.removeItem(storageKey(userId)) } catch { /* ignore storage failures */ }
    return null
  }
}

export function writeMarketWatchCache(userId: string | undefined, snapshot: MarketWatchSnapshot): void {
  if (typeof window === 'undefined' || snapshot.state !== 'ready') return
  try {
    window.localStorage.setItem(storageKey(userId), JSON.stringify({ ...snapshot, fromCache: false }))
  } catch {
    // A disabled or full localStorage should not affect the current chat run.
  }
}

function storageKey(userId: string | undefined): string {
  return `wyckoff:${userId || 'guest'}:${MARKET_WATCH_STORAGE_VERSION}`
}
