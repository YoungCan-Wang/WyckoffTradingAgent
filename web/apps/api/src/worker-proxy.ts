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

export function isPagesFallbackPath(pathname: string): boolean {
  return pathname === '/api/shadow-ledger' || pathname.startsWith('/api/shadow-ledger/')
}

export async function handleWorkerProxyRequest(
  request: Request,
  env?: WorkerProxyEnv,
  localFetch?: (request: Request) => Promise<Response> | Response,
): Promise<Response> {
  const pathname = new URL(request.url).pathname
  if (!isWorkerProxyPath(pathname)) {
    return Response.json({ error: 'Not Found' }, { status: 404 })
  }
  try {
    const proxied = await proxyToWorker(request, env)
    if (proxied.status !== 404 || !localFetch || !isPagesFallbackPath(pathname)) {
      return proxied
    }
    return localFetch(request)
  } catch (error) {
    if (localFetch && isPagesFallbackPath(pathname)) return localFetch(request)
    throw error
  }
}
