import { describe, expect, it } from 'vitest'
import {
  getChatContextWindow,
  getChatRecentKeepTokens,
  prepareChatMessagesForModel,
} from '../chat-agent'

function makeHistory(turns: number) {
  const messages: { role: 'user' | 'assistant'; content: string }[] = []
  for (let i = 0; i < turns; i += 1) {
    messages.push({ role: 'user', content: `帮我看看 600${String(i % 10).padStart(3, '0')} ${'量价关系 '.repeat(120)}` })
    messages.push({ role: 'assistant', content: `结论 ${i}: 先看供需，不追涨。 ${'等待确认 '.repeat(120)}` })
  }
  return messages
}

describe('reading-room context preparation', () => {
  it('uses the official DeepSeek V4 one-million-token context window', () => {
    expect(getChatContextWindow('deepseek-v4-flash')).toBe(1_000_000)
    expect(getChatContextWindow('deepseek-v4-pro')).toBe(1_000_000)
    expect(getChatContextWindow('deepseek-chat')).toBe(64_000)
    expect(getChatContextWindow('deepseek-reasoner')).toBe(64_000)
  })

  it('scales recent token budget by model', () => {
    expect(getChatRecentKeepTokens('gpt-3.5-turbo')).toBe(4000)
    expect(getChatRecentKeepTokens('deepseek-v4-flash')).toBe(20000)
    expect(getChatRecentKeepTokens('claude-sonnet-4')).toBe(20000)
  })

  it('compacts long chat history into summary plus recent tail', () => {
    const messages = makeHistory(16)
    const prepared = prepareChatMessagesForModel(messages, 'gpt-3.5-turbo')

    expect(prepared.compacted).toBe(true)
    expect(prepared.messages[0]?.content).toContain('[读盘室对话摘要]')
    expect(prepared.messages[0]?.content).toContain('工具实时返回')
    expect(prepared.messages.at(-1)).toEqual(messages.at(-1))
    expect(prepared.afterMessages).toBeLessThan(prepared.beforeMessages)
    expect(prepared.afterTokens).toBeLessThan(prepared.beforeTokens)
  })

  it('keeps small chat history unchanged', () => {
    const messages = [
      { role: 'user' as const, content: '帮我看看 000001' },
      { role: 'assistant' as const, content: '先取数据再判断。' },
    ]
    const prepared = prepareChatMessagesForModel(messages, 'gpt-3.5-turbo')

    expect(prepared.compacted).toBe(false)
    expect(prepared.messages).toEqual(messages)
  })
})
