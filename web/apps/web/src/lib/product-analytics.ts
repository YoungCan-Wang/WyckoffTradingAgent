const CF_BEACON_ID = 'cf-web-analytics'
const CLARITY_SCRIPT_ID = 'ms-clarity'

export function cloudflareWebAnalyticsToken(): string {
  return String(import.meta.env.VITE_CF_WEB_ANALYTICS_TOKEN || '').trim()
}

export function clarityProjectId(): string {
  return String(import.meta.env.VITE_CLARITY_PROJECT_ID || '').trim()
}

export function installCloudflareWebAnalytics(doc?: Document): void {
  const token = cloudflareWebAnalyticsToken()
  const target = doc ?? (typeof document === 'undefined' ? undefined : document)
  if (!token || !target || target.getElementById(CF_BEACON_ID)) return
  const script = target.createElement('script')
  script.id = CF_BEACON_ID
  script.defer = true
  script.src = 'https://static.cloudflareinsights.com/beacon.min.js'
  script.setAttribute('data-cf-beacon', JSON.stringify({ token }))
  target.head.appendChild(script)
}

export function installWhitelistClarity(userId: string, doc?: Document, win?: Window): void {
  const projectId = clarityProjectId()
  const target = doc ?? (typeof document === 'undefined' ? undefined : document)
  const host = win ?? (typeof window === 'undefined' ? undefined : window)
  if (!projectId || !userId.trim() || !target || !host || target.getElementById(CLARITY_SCRIPT_ID)) return
  const clarity = ensureClarityQueue(host)
  clarity('identify', userId)
  const script = target.createElement('script')
  script.id = CLARITY_SCRIPT_ID
  script.async = true
  script.src = `https://www.clarity.ms/tag/${encodeURIComponent(projectId)}`
  target.head.appendChild(script)
}

type ClarityFn = ((...args: unknown[]) => void) & { q?: unknown[] }

function ensureClarityQueue(win: Window): ClarityFn {
  const current = (win as Window & { clarity?: ClarityFn }).clarity
  if (current) return current
  const queued: ClarityFn = (...args: unknown[]) => {
    queued.q = queued.q || []
    queued.q.push(args)
  }
  ;(win as Window & { clarity?: ClarityFn }).clarity = queued
  return queued
}
