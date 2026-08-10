import { describe, expect, it } from 'vitest'
import { appendMarketWatchModelMessage, buildStableChatSystemPrompt } from '../services/chat-prompt-prefix'

describe('reading-room prompt cache prefix', () => {
  it('keeps system prompt free of market-watch quotes', () => {
    const system = buildStableChatSystemPrompt({
      rolePrompt: 'STATIC_SYSTEM',
      webSearchGuidance: 'WEB_SEARCH_GUIDANCE',
    })
    expect(system).toBe('STATIC_SYSTEM\n\nWEB_SEARCH_GUIDANCE')
    expect(system).not.toContain('观察篮')
  })

  it('omits empty web-search guidance', () => {
    expect(buildStableChatSystemPrompt({ rolePrompt: 'STATIC_SYSTEM' })).toBe('STATIC_SYSTEM')
  })

  it('appends market watch as a trailing user message', () => {
    const messages = [
      { role: 'user' as const, content: '看看 AAPL' },
      { role: 'assistant' as const, content: '好的' },
      { role: 'user' as const, content: '复盘观察篮' },
    ]
    const next = appendMarketWatchModelMessage(messages, '## 观察篮临时行情\nAAPL.US | 最新价=200')
    expect(next).toHaveLength(4)
    expect(next[3]).toEqual({ role: 'user', content: '## 观察篮临时行情\nAAPL.US | 最新价=200' })
    expect(next.slice(0, 3)).toEqual(messages)
  })

  it('skips empty market watch context', () => {
    const messages = [{ role: 'user' as const, content: '你好' }]
    expect(appendMarketWatchModelMessage(messages, '   ')).toEqual(messages)
  })
})
