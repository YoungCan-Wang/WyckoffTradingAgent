import {
  normalizeGeminiStream,
} from '../../../packages/shared/src/gemini-sse-normalize'
import { ALLOWED_PROXY_TARGET_ORIGINS } from '../../../packages/shared/src/constants'

const CORS_HEADERS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
  'Access-Control-Allow-Headers': '*',
  'Access-Control-Max-Age': '86400',
}

const FORWARD_HEADERS = new Set([
  'authorization',
  'content-type',
  'accept',
  'x-api-key',
  'anthropic-version',
])

const ALLOWED_TARGET_ORIGINS: Set<string> = new Set(ALLOWED_PROXY_TARGET_ORIGINS)

const JSON_CONTENT_RE = /\bapplication\/json\b/i
const SSE_CONTENT_RE = /\btext\/event-stream\b/i
const ONE_ROUTE_ORIGINS = new Set(['https://api.1route.dev', 'https://www.1route.dev'])

function normalizeTargetUrl(raw: string): URL | null {
  try {
    const url = new URL(raw)
    if (!['https:', 'http:'].includes(url.protocol)) return null
    if (!ALLOWED_TARGET_ORIGINS.has(url.origin)) return null
    return url
  } catch {
    return null
  }
}

function joinTargetUrl(targetUrl: URL, proxyPath: string, search: string): string {
  const base = targetUrl.href.replace(/\/$/, '')
  return `${base}${proxyPath}${search}`
}

function isOneRouteChatCompletion(targetUrl: URL, proxyPath: string): boolean {
  return ONE_ROUTE_ORIGINS.has(targetUrl.origin) && proxyPath.endsWith('/chat/completions')
}

function isGeminiChatCompletion(targetUrl: URL, proxyPath: string): boolean {
  return targetUrl.origin === 'https://generativelanguage.googleapis.com' && proxyPath.endsWith('/chat/completions')
}

function buildOneRouteChatBody(body: ArrayBuffer, contentType: string): BodyInit {
  if (!JSON_CONTENT_RE.test(contentType) || body.byteLength === 0) return body

  try {
    const payload = JSON.parse(new TextDecoder().decode(body)) as Record<string, unknown>
    delete payload.stream_options

    if (Array.isArray(payload.messages)) {
      payload.messages = payload.messages.map((message) => {
        if (!message || typeof message !== 'object' || Array.isArray(message)) return message
        const item = message as Record<string, unknown>
        return item.role === 'developer' ? { ...item, role: 'system' } : item
      })
    }

    return JSON.stringify(payload)
  } catch {
    return body
  }
}

export const onRequest: PagesFunction = async (context) => {
  const { request } = context

  if (request.method === 'OPTIONS') {
    return new Response(null, { headers: CORS_HEADERS })
  }

  const targetUrl = request.headers.get('X-Target-URL')
  if (!targetUrl) {
    return Response.json({ error: 'Missing X-Target-URL header' }, { status: 400 })
  }
  const target = normalizeTargetUrl(targetUrl)
  if (!target) {
    return Response.json({ error: 'X-Target-URL is not allowed' }, { status: 403, headers: CORS_HEADERS })
  }

  const url = new URL(request.url)
  const proxyPath = url.pathname.replace('/api/llm-proxy', '')
  const dest = joinTargetUrl(target, proxyPath, url.search)
  const body = request.method !== 'GET' && request.method !== 'HEAD'
    ? await request.arrayBuffer()
    : undefined

  const headers = new Headers()
  request.headers.forEach((value, key) => {
    if (FORWARD_HEADERS.has(key)) headers.set(key, value)
  })
  headers.set('user-agent', 'wyckoff-agent/1.0')

  try {
    const requestBody = body && isOneRouteChatCompletion(target, proxyPath)
      ? buildOneRouteChatBody(body, headers.get('content-type') || '')
      : body
    const response = await fetch(dest, {
      method: request.method,
      headers,
      body: requestBody,
    })

    const respHeaders = new Headers()
    response.headers.forEach((value, key) => {
      if (!['transfer-encoding', 'content-encoding'].includes(key)) {
        respHeaders.set(key, value)
      }
    })
    respHeaders.set('Access-Control-Allow-Origin', '*')
    respHeaders.set('X-Wyckoff-Proxy-Target', target.origin)

    const respBody = response.body
    const contentType = response.headers.get('content-type') || ''
    const responseBody = respBody && isGeminiChatCompletion(target, proxyPath) && SSE_CONTENT_RE.test(contentType)
      ? normalizeGeminiStream(respBody)
      : respBody

    return new Response(responseBody, {
      status: response.status,
      headers: respHeaders,
    })
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err)
    return Response.json({ error: { message: `Proxy error: ${msg}` } }, { status: 502 })
  }
}
