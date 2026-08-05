import { describe, expect, it } from 'vitest'
import { formatToolName, toolProgressDescription, toolResultDigest } from '@/features/reading-room/tool-rendering-model'
import type { ToolPart } from '@/features/reading-room/messages'

describe('web_search tool rendering', () => {
  it('uses localized label', () => {
    expect(formatToolName('web_search', (key) => key === 'tool.web_search' ? '联网搜索' : key)).toBe('联网搜索')
  })

  it('uses empty-input fallback while running and output action after completion', () => {
    expect(toolProgressDescription('web_search', {})).toBe('服务端联网检索公开信息。')

    const donePart = {
      type: 'tool-web_search',
      toolCallId: 'call_1',
      state: 'output-available',
      input: {},
      output: {
        action: { type: 'search', query: '大众交通 涨停' },
        sources: [{ type: 'url', url: 'https://example.com/a' }, { type: 'url', url: 'https://example.com/b' }],
      },
    } as ToolPart

    expect(toolProgressDescription('web_search', {}, donePart)).toContain('大众交通 涨停')
    expect(toolResultDigest('web_search', donePart.output)).toBe('联网搜索完成：大众交通 涨停 · 2 个来源')
  })
})
