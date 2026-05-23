import type { LLMConfig } from './chat-agent'

interface ChatMessage {
  role: 'system' | 'user' | 'assistant'
  content: string
}

type StreamProtocol = 'openai' | 'anthropic'

interface StreamRequest {
  url: string
  headers: Record<string, string>
  body: string
}

export async function streamLLMResponse(
  config: LLMConfig,
  messages: ChatMessage[],
  opts: { temperature?: number; maxTokens?: number; signal?: AbortSignal; onDelta?: (chunk: string) => void } = {},
): Promise<string> {
  const protocol = config.protocol ?? 'openai'
  const request = buildStreamRequest(config, messages, opts, protocol)
  const response = await fetch(request.url, {
    method: 'POST',
    signal: opts.signal,
    headers: request.headers,
    body: request.body,
  })

  if (!response.ok) {
    const err = await response.json().catch(() => ({}))
    throw new Error(err.error?.message || `模型请求失败 (${response.status})`)
  }

  const reader = response.body?.getReader()
  if (!reader) throw new Error('响应无可读流')

  const decoder = new TextDecoder()
  let result = ''
  let buffer = ''

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop()!
    for (const line of lines) {
      const delta = extractDataLineDelta(line, protocol)
      if (delta) { opts.onDelta?.(delta); result += delta }
    }
  }
  buffer += decoder.decode()
  for (const line of buffer.split('\n')) {
    const delta = extractDataLineDelta(line, protocol)
    if (delta) { opts.onDelta?.(delta); result += delta }
  }
  return result
}

function buildStreamRequest(
  config: LLMConfig,
  messages: ChatMessage[],
  opts: { temperature?: number; maxTokens?: number },
  protocol: StreamProtocol,
): StreamRequest {
  if (protocol === 'anthropic') {
    const system = messages.filter(item => item.role === 'system').map(item => item.content).join('\n\n')
    const chatMessages = messages.filter(item => item.role !== 'system')
    return {
      url: '/api/llm-proxy/v1/messages',
      headers: {
        'Content-Type': 'application/json',
        'x-api-key': config.api_key,
        'anthropic-version': '2023-06-01',
        'X-Target-URL': config.base_url,
      },
      body: JSON.stringify({
        model: config.model,
        messages: chatMessages,
        ...(system ? { system } : {}),
        temperature: opts.temperature ?? 0.5,
        max_tokens: opts.maxTokens ?? 4096,
        stream: true,
      }),
    }
  }

  return {
    url: '/api/llm-proxy/chat/completions',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${config.api_key}`,
      'X-Target-URL': config.base_url,
    },
    body: JSON.stringify({
      model: config.model,
      messages,
      temperature: opts.temperature ?? 0.5,
      max_tokens: opts.maxTokens ?? 4096,
      stream: true,
    }),
  }
}

function extractDataLineDelta(line: string, protocol: StreamProtocol): string | undefined {
  const trimmed = line.trim()
  if (!trimmed.startsWith('data: ')) return undefined
  const payload = trimmed.slice(6)
  if (payload === '[DONE]') return undefined
  try {
    return extractStreamDelta(JSON.parse(payload), protocol)
  } catch {
    return undefined
  }
}

function extractStreamDelta(json: unknown, protocol: StreamProtocol): string | undefined {
  if (!json || typeof json !== 'object') return undefined
  if (protocol === 'anthropic') return extractAnthropicDelta(json as Record<string, unknown>)
  const choices = (json as Record<string, unknown>).choices
  if (!Array.isArray(choices)) return undefined
  const first = choices[0]
  if (!first || typeof first !== 'object') return undefined
  const delta = (first as Record<string, unknown>).delta
  if (!delta || typeof delta !== 'object') return undefined
  const content = (delta as Record<string, unknown>).content
  return typeof content === 'string' ? content : undefined
}

function extractAnthropicDelta(json: Record<string, unknown>): string | undefined {
  if (json.type !== 'content_block_delta') return undefined
  const delta = json.delta
  if (!delta || typeof delta !== 'object') return undefined
  const text = (delta as Record<string, unknown>).text
  return typeof text === 'string' ? text : undefined
}
