const LOCAL_API_URL = 'http://127.0.0.1:8787'

export type ApiUrlEnv = {
  VITE_API_URL?: string
  DEV: boolean
}

export function apiUrl(path: `/api/${string}`, env: ApiUrlEnv = import.meta.env): string {
  const configured = env.VITE_API_URL?.trim()
  if (configured) return `${configured.replace(/\/$/, '')}${path}`
  if (env.DEV) return `${LOCAL_API_URL}${path}`
  return path
}

export function toWebSocketUrl(httpUrl: string, pageOrigin?: string): string {
  if (httpUrl.startsWith('http://') || httpUrl.startsWith('https://')) {
    return httpUrl.replace(/^http/, 'ws')
  }
  if (pageOrigin) return `${pageOrigin.replace(/^http/, 'ws')}${httpUrl}`
  return httpUrl
}

export function apiWsUrl(path: `/api/${string}`, env: ApiUrlEnv = import.meta.env): string {
  return toWebSocketUrl(apiUrl(path, env), globalThis.location?.origin)
}
