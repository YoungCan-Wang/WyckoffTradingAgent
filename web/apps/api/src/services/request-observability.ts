const SECRET_FRAGMENT = /bearer\s+[a-z0-9._~+/-]+=*|eyj[a-z0-9_-]{20,}/gi

export function logUnhandledWorkerError(
  error: unknown,
  request: { get: (key: string) => unknown; req: { method: string; path: string } },
): void {
  console.error(JSON.stringify({
    event: 'worker_error',
    timestamp: new Date().toISOString(),
    requestId: readRequestId(request),
    method: request.req.method,
    path: request.req.path,
    userId: readOptionalUserId(request),
    error: errorName(error),
    message: sanitizeLogText(errorMessage(error)),
  }))
}

export function readOptionalUserId(request: { get: (key: string) => unknown }): string | undefined {
  const auth = request.get('auth')
  if (!auth || typeof auth !== 'object' || !('userId' in auth)) return undefined
  const userId = (auth as { userId?: unknown }).userId
  return typeof userId === 'string' && userId.trim() ? userId : undefined
}

export function sanitizeLogText(value: string, limit = 200): string {
  return value.replace(SECRET_FRAGMENT, '[redacted]').replace(/\s+/g, ' ').trim().slice(0, limit)
}

function readRequestId(request: { get: (key: string) => unknown }): string | undefined {
  const requestId = request.get('requestId')
  return typeof requestId === 'string' && requestId.trim() ? requestId : undefined
}

function errorName(error: unknown): string {
  return error instanceof Error && error.name ? error.name : 'Error'
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message
  return typeof error === 'string' ? error : 'unknown_error'
}
