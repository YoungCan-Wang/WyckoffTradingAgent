import { afterEach, describe, expect, it, vi } from 'vitest'
import { streamLLMResponse } from '../llm-stream'
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
})
