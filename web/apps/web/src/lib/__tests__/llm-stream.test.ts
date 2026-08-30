import { afterEach, describe, expect, it, vi } from 'vitest'
import { streamLLMResponse, streamLLMResponseWithFallback } from '../llm-stream'
import type { LLMConfig } from '../chat-agent'

function sseResponse(lines: string[]): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      const encoder = new TextEncoder()
      controller.enqueue(encoder.encode(lines.join('\n')))
      controller.close()
    },
  })
  return new Response(stream, { status: 200, headers: { 'content-type': 'text/event-stream' } })
}

function fetchCall(): [string, RequestInit] {
  const mock = vi.mocked(fetch)
  return mock.mock.calls[0] as [string, RequestInit]
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('streamLLMResponse', () => {
  it('streams OpenAI-compatible chat completions', async () => {
    const config: LLMConfig = {
      api_key: 'openai-key',
      model: 'gpt-test',
      base_url: 'https://api.openai.com/v1',
      protocol: 'openai',
    }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(sseResponse([
      'data: {"choices":[{"delta":{"content":"hello"}}]}',
      'data: {"choices":[{"delta":{"content":" world"}}]}',
      'data: [DONE]',
      '',
    ])))

    const result = await streamLLMResponse(config, [{ role: 'user', content: 'hi' }])
    const [url, init] = fetchCall()

    expect(result).toBe('hello world')
    expect(url).toBe('/api/llm-proxy/chat/completions')
    expect(init.headers).toMatchObject({
      Authorization: 'Bearer openai-key',
      'X-Target-URL': 'https://api.openai.com/v1',
    })
    expect(JSON.parse(String(init.body))).toMatchObject({ model: 'gpt-test', stream: true })
  })

  it('replays DeepSeek reasoning when a reasoning-only segment reaches the output limit', async () => {
    const config: LLMConfig = {
      provider: 'deepseek',
      api_key: 'deepseek-key',
      model: 'deepseek-v4-flash',
      base_url: 'https://api.deepseek.com/v1',
      protocol: 'openai',
    }
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(sseResponse([
        'data: {"choices":[{"delta":{"reasoning_content":"推理中"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"length"}]}',
        'data: [DONE]',
      ]))
      .mockResolvedValueOnce(sseResponse([
        'data: {"choices":[{"delta":{"content":"完整正文"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        'data: [DONE]',
      ]))
    vi.stubGlobal('fetch', fetchMock)

    const result = await streamLLMResponse(config, [{ role: 'user', content: '分析' }], { maxTokens: 4000 })
    const firstBody = JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))
    const secondBody = JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body))

    expect(result).toBe('完整正文')
    expect(firstBody).toMatchObject({
      max_tokens: 12000,
      thinking: { type: 'enabled' },
      reasoning_effort: 'low',
    })
    expect(firstBody).not.toHaveProperty('temperature')
    expect(secondBody.messages.at(-2)).toEqual({ role: 'assistant', content: '', reasoning_content: '推理中' })
  })

  it('streams Anthropic messages through the proxy with Anthropic headers', async () => {
    const config: LLMConfig = {
      api_key: 'anthropic-key',
      model: 'claude-test',
      base_url: 'https://api.anthropic.com',
      protocol: 'anthropic',
    }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(sseResponse([
      'event: content_block_delta',
      'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"alpha"}}',
      'event: content_block_delta',
      'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":" beta"}}',
      'data: [DONE]',
      '',
    ])))

    const result = await streamLLMResponse(config, [
      { role: 'system', content: 'system prompt' },
      { role: 'user', content: 'hi' },
    ])
    const [url, init] = fetchCall()
    const body = JSON.parse(String(init.body))

    expect(result).toBe('alpha beta')
    expect(url).toBe('/api/llm-proxy/v1/messages')
    expect(init.headers).toMatchObject({
      'x-api-key': 'anthropic-key',
      'anthropic-version': '2023-06-01',
      'X-Target-URL': 'https://api.anthropic.com',
    })
    expect(init.headers).not.toHaveProperty('Authorization')
    expect(body).toMatchObject({ model: 'claude-test', system: 'system prompt', stream: true })
    expect(body.messages).toEqual([{ role: 'user', content: 'hi' }])
  })

  it('flushes the final SSE line when the stream has no trailing newline', async () => {
    const config: LLMConfig = {
      api_key: 'anthropic-key',
      model: 'claude-test',
      base_url: 'https://api.anthropic.com',
      protocol: 'anthropic',
    }
    const onDelta = vi.fn()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(sseResponse([
      'event: content_block_delta',
      'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"last chunk"}}',
    ])))

    const result = await streamLLMResponse(config, [{ role: 'user', content: 'hi' }], { onDelta })

    expect(result).toBe('last chunk')
    expect(onDelta).toHaveBeenCalledWith('last chunk')
  })

  it('retries a transient model failure without duplicating streamed deltas', async () => {
    const config: LLMConfig = {
      api_key: 'openai-key',
      model: 'gpt-test',
      base_url: 'https://api.openai.com/v1',
      protocol: 'openai',
    }
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ error: { message: 'busy' } }), { status: 503 }))
      .mockResolvedValueOnce(sseResponse(['data: {"choices":[{"delta":{"content":"recovered"}}]}', 'data: [DONE]']))
    vi.stubGlobal('fetch', fetchMock)
    const onDelta = vi.fn()
    const onStatus = vi.fn()

    const result = await streamLLMResponseWithFallback([config], [{ role: 'user', content: 'hi' }], { onDelta, onStatus })

    expect(result).toBe('recovered')
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(onDelta).toHaveBeenCalledTimes(1)
    expect(onStatus).toHaveBeenCalledWith(expect.objectContaining({ phase: 'retrying', attempt: 1 }))
  })
})
