import { useEffect } from 'react'
import { APP_VERSION } from '@/lib/app-version'

const VERSION_URL = '/version.json'
const CHECK_INTERVAL_MS = 60_000
const RELOAD_SESSION_KEY = 'wyckoff.update.reload'

interface VersionPayload {
  version?: unknown
}

export function AppUpdateGate() {
  useEffect(() => {
    let disposed = false
    let checking = false
    const check = async () => {
      if (checking || disposed) return
      checking = true
      try {
        const remoteVersion = await fetchRemoteVersion()
        if (!disposed && shouldReload(remoteVersion) && !isReadingRoomStreaming()) reloadForVersion(remoteVersion)
      } finally {
        checking = false
      }
    }
    const handleFocus = () => { void check() }
    const handleVisibility = () => {
      if (document.visibilityState === 'visible') void check()
    }

    const intervalId = window.setInterval(() => { void check() }, CHECK_INTERVAL_MS)
    window.addEventListener('focus', handleFocus)
    document.addEventListener('visibilitychange', handleVisibility)
    void check()

    return () => {
      disposed = true
      window.clearInterval(intervalId)
      window.removeEventListener('focus', handleFocus)
      document.removeEventListener('visibilitychange', handleVisibility)
    }
  }, [])

  return null
}

async function fetchRemoteVersion(): Promise<string | null> {
  try {
    const response = await fetch(`${VERSION_URL}?t=${Date.now()}`, { cache: 'no-store' })
    if (!response.ok) return null
    const payload = await response.json() as VersionPayload
    return typeof payload.version === 'string' && payload.version.trim() ? payload.version : null
  } catch {
    return null
  }
}

function shouldReload(remoteVersion: string | null): remoteVersion is string {
  return remoteVersion !== null && remoteVersion !== APP_VERSION
}

function isReadingRoomStreaming(): boolean {
  return window.location.pathname === '/chat'
    && document.querySelector('[data-reading-room-streaming="true"]') !== null
}

function reloadForVersion(remoteVersion: string): void {
  let shouldReload = true
  try {
    const marker = `${APP_VERSION}->${remoteVersion}`
    shouldReload = window.sessionStorage.getItem(RELOAD_SESSION_KEY) !== marker
    if (shouldReload) window.sessionStorage.setItem(RELOAD_SESSION_KEY, marker)
  } catch {
    // Keep the default reload path when sessionStorage is unavailable.
  }
  if (shouldReload) window.location.reload()
}
