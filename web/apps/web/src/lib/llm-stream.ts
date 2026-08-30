import {
  DEEPSEEK_REPORT_MAX_OUTPUT_TOKENS,
  deepSeekThinkingBody,
  isOfficialDeepSeek,
} from '@wyckoff/shared'
import type { LLMConfig } from './chat-agent'

interface ChatMessage {
  role: 'system' | 'user' | 'assistant'
  content: string
  reasoning_content?: string
}

type StreamProtocol = 'openai' | 'anthropic'

interface StreamRequest {
  url: string
  headers: Record<string, string>
  body: string
}

interface StreamSegment {
  text: string
  reasoning: string
  finishReason: string
}

export interface LLMStreamStatus {
  phase: 'retrying' | 'fallback'
  model: string
  attempt: number
  nextModel?: string
}

const DEEPSEEK_MAX_SEGMENTS = 3
const DEEPSEEK_CONTINUATION_PROMPT = '继续完成上一段回答，不要重复已经给出的内容。'

export async function streamLLMResponse(
  config: LLMConfig,
  messages: ChatMessage[],
  opts: { temperature?: number; maxTokens?: number; signal?: AbortSignal; onDelta?: (chunk: string) => void } = {},
): Promise<string> {
  const protocol = config.protocol ?? 'openai'
  const officialDeepSeek = isOfficialDeepSeek(config.provider || 'deepseek', config.model, config.base_url)
  const maxSegments = officialDeepSeek && protocol === 'openai' ? DEEPSEEK_MAX_SEGMENTS : 1
  let requestMessages = [...messages]
  let result = ''

  for (let index = 0; index < maxSegments; index += 1) {
    const request = buildStreamRequest(config, requestMessages, opts, protocol)
    const segment = await fetchStreamSegment(request, protocol, opts)
    result += segment.text
    if (segment.finishReason !== 'length') {
      if (!result.trim()) throw new Error('模型未返回正文')
      return result
    }
    if (index + 1 >= maxSegments) {
      throw new Error(`模型连续 ${maxSegments} 段达到输出上限，未能生成完整正文`)
    }
    requestMessages = appendDeepSeekContinuation(requestMessages, segment)
  }
  throw new Error('模型未返回完整正文')
}

async function fetchStreamSegment(
  request: StreamRequest,
  protocol: StreamProtocol,
  opts: { signal?: AbortSignal; onDelta?: (chunk: string) => void },
): Promise<StreamSegment> {
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
  return readStreamSegment(reader, protocol, opts.onDelta)
}

async function readStreamSegment(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  protocol: StreamProtocol,
  onDelta?: (chunk: string) => void,
): Promise<StreamSegment> {
  const decoder = new TextDecoder()
  const segment: StreamSegment = { text: '', reasoning: '', finishReason: '' }
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop()!
    for (const line of lines) consumeDataLine(line, protocol, segment, onDelta)
  }
  buffer += decoder.decode()
  for (const line of buffer.split('\n')) consumeDataLine(line, protocol, segment, onDelta)
  return segment
}

function appendDeepSeekContinuation(messages: ChatMessage[], segment: StreamSegment): ChatMessage[] {
  return [
    ...messages,
    {
      role: 'assistant',
      content: segment.text,
      ...(segment.reasoning ? { reasoning_content: segment.reasoning } : {}),
    },
    { role: 'user', content: DEEPSEEK_CONTINUATION_PROMPT },
  ]
}

export async function streamLLMResponseWithFallback(
  configs: LLMConfig[],
  messages: ChatMessage[],
  opts: { temperature?: number; maxTokens?: number; signal?: AbortSignal; onDelta?: (chunk: string) => void; onStatus?: (status: LLMStreamStatus) => void } = {},
): Promise<string> {
  let lastError: unknown = new Error('没有可用的模型配置')
  for (let index = 0; index < configs.length; index += 1) {
    const config = configs[index]
    if (!config) continue
    for (let attempt = 1; attempt <= 2; attempt += 1) {
      let emitted = false
      try {
        return await streamLLMResponse(config, messages, {
          ...opts,
          onDelta: (chunk) => { emitted = true; opts.onDelta?.(chunk) },
        })
      } catch (error) {
        lastError = error
        if (opts.signal?.aborted || emitted || !isRetryableModelError(error)) throw error
        const nextConfig = configs[index + 1]
        const canRetry = attempt < 2
        opts.onStatus?.({
          phase: canRetry ? 'retrying' : 'fallback',
          model: config.model,
          attempt,
          ...(canRetry ? {} : nextConfig ? { nextModel: nextConfig.model } : {}),
        })
        if (!canRetry && !nextConfig) throw error
        await waitForRetry(attempt, opts.signal)
        if (!canRetry) break
      }
    }
  }
  throw lastError
}

function isRetryableModelError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error)
  const match = message.match(/\((\d{3})\)/)
  const status = match ? Number(match[1]) : null
  if (status == null) return true
  return status === 408 || status === 409 || status === 429 || status >= 500
}

async function waitForRetry(attempt: number, signal?: AbortSignal): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    const timer = globalThis.setTimeout(resolve, attempt * 350)
    signal?.addEventListener('abort', () => { globalThis.clearTimeout(timer); reject(signal.reason) }, { once: true })
  })
}

function buildStreamRequest(
  config: LLMConfig,
  messages: ChatMessage[],
  opts: { temperature?: number; maxTokens?: number },
  protocol: StreamProtocol,
): StreamRequest {
  if (protocol === 'anthropic') return buildAnthropicRequest(config, messages, opts)
  const officialDeepSeek = isOfficialDeepSeek(config.provider || 'deepseek', config.model, config.base_url)
  const maxTokens = officialDeepSeek
    ? Math.max(opts.maxTokens ?? 0, DEEPSEEK_REPORT_MAX_OUTPUT_TOKENS)
    : opts.maxTokens ?? 4096
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
      ...(!officialDeepSeek ? { temperature: opts.temperature ?? 0.5 } : {}),
      ...(officialDeepSeek ? deepSeekThinkingBody('low') : {}),
      max_tokens: maxTokens,
      stream: true,
    }),
  }
}

function buildAnthropicRequest(
  config: LLMConfig,
  messages: ChatMessage[],
  opts: { temperature?: number; maxTokens?: number },
): StreamRequest {
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

function consumeDataLine(
  line: string,
  protocol: StreamProtocol,
  segment: StreamSegment,
  onDelta?: (chunk: string) => void,
): void {
  const trimmed = line.trim()
  if (!trimmed.startsWith('data: ')) return
  const payload = trimmed.slice(6)
  if (payload === '[DONE]') return
  try {
    const delta = extractStreamDelta(JSON.parse(payload), protocol)
    if (delta.text) {
      segment.text += delta.text
      onDelta?.(delta.text)
    }
    if (delta.reasoning) segment.reasoning += delta.reasoning
    if (delta.finishReason) segment.finishReason = delta.finishReason
  } catch {
    return
  }
}

function extractStreamDelta(json: unknown, protocol: StreamProtocol): Partial<StreamSegment> {
  if (!json || typeof json !== 'object') return {}
  if (protocol === 'anthropic') return { text: extractAnthropicDelta(json as Record<string, unknown>) }
  const choices = (json as Record<string, unknown>).choices
  if (!Array.isArray(choices)) return {}
  const first = choices[0]
  if (!first || typeof first !== 'object') return {}
  const choice = first as Record<string, unknown>
  const finishReason = typeof choice.finish_reason === 'string' ? choice.finish_reason : undefined
  const delta = choice.delta
  if (!delta || typeof delta !== 'object') return { finishReason }
  const values = delta as Record<string, unknown>
  return {
    text: typeof values.content === 'string' ? values.content : undefined,
    reasoning: typeof values.reasoning_content === 'string' ? values.reasoning_content : undefined,
    finishReason,
  }
}

function extractAnthropicDelta(json: Record<string, unknown>): string | undefined {
  if (json.type !== 'content_block_delta') return undefined
  const delta = json.delta
  if (!delta || typeof delta !== 'object') return undefined
  const text = (delta as Record<string, unknown>).text
  return typeof text === 'string' ? text : undefined
}
