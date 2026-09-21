export const DEFAULT_WORKER_ORIGIN = 'https://wyckoff-api.yongkai-wang.workers.dev'

export const WORKER_PROXY_PREFIXES = [
  '/api/chat',
  '/api/settings',
  '/api/portfolio',
  '/api/shadow-ledger',
  '/api/agent-runs',
  '/api/remote',
  '/api/health',
] as const

export type WorkerProxyEnv = {
  WYCKOFF_API_ORIGIN?: string
}

export function resolveWorkerOrigin(env?: WorkerProxyEnv): string {
  const configured = env?.WYCKOFF_API_ORIGIN?.trim()
  return (configured || DEFAULT_WORKER_ORIGIN).replace(/\/$/, '')
}

export function isWorkerProxyPath(pathname: string): boolean {
  return WORKER_PROXY_PREFIXES.some((prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`))
}

export function workerProxyUrl(requestUrl: string, origin: string): string {
  const incoming = new URL(requestUrl)
  return `${origin.replace(/\/$/, '')}${incoming.pathname}${incoming.search}`
}

export function proxyToWorker(request: Request, env?: WorkerProxyEnv): Promise<Response> {
  return fetch(workerProxyUrl(request.url, resolveWorkerOrigin(env)), request)
}

export function handleWorkerProxyRequest(request: Request, env?: WorkerProxyEnv): Promise<Response> {
  const pathname = new URL(request.url).pathname
  if (!isWorkerProxyPath(pathname)) {
    return Promise.resolve(Response.json({ error: 'Not Found' }, { status: 404 }))
  }
  return proxyToWorker(request, env)
}
