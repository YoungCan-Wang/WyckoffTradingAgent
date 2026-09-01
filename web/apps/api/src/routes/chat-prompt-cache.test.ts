import { describe, expect, it } from 'vitest'
import { assessTradingDay, formatSessionClockContext, resolveSessionClock } from '@wyckoff/shared'
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

  it('时间口径也走 user 消息 —— 写进 system 会每轮击穿缓存', () => {
    const clock = resolveSessionClock(new Date('2026-08-28T02:00:00Z'))
    const context = formatSessionClockContext(clock, assessTradingDay(clock, ['2026-08-28T02:00:00Z']))
    const system = buildStableChatSystemPrompt({ rolePrompt: 'STATIC_SYSTEM' })
    expect(system).not.toContain('北京时间')

    const next = appendMarketWatchModelMessage([{ role: 'user' as const, content: '看看 600519' }], context)
    expect(next).toHaveLength(2)
    expect(next[1]).toMatchObject({ role: 'user' })
    expect((next[1] as { content: string }).content).toContain('当前北京时间:2026-08-28 10:00(UTC+8)')
  })

  it('时间与行情各占一条 user 消息，顺序是时间在前', () => {
    const next = appendMarketWatchModelMessage(
      appendMarketWatchModelMessage([{ role: 'user' as const, content: '复盘' }], '## 当前时间与交易时段'),
      '## 观察篮临时行情',
    )
    expect(next.map((message) => (message as { content: string }).content)).toEqual([
      '复盘', '## 当前时间与交易时段', '## 观察篮临时行情',
    ])
  })
})
