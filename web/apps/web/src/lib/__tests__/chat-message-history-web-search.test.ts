import { describe, expect, it } from 'vitest'
import type { UIMessage } from 'ai'
import { sanitizeMessagesForChatTransport } from '@wyckoff/shared'

describe('sanitizeMessagesForChatTransport', () => {
  it('collapses provider-executed web_search parts into text for chat transport', () => {
    const messages = [
      {
        id: 'a1',
        role: 'assistant',
        parts: [
          {
            type: 'tool-web_search',
            toolCallId: 'call_1',
            state: 'output-available',
            providerExecuted: true,
            input: {},
            output: {
              action: { type: 'search', query: '大众交通 涨停' },
              sources: [{ type: 'url', url: 'https://example.com/a' }],
            },
          },
          { type: 'text', text: '结论：有公开报道。' },
        ],
      },
    ] as unknown as UIMessage[]

    const sanitized = sanitizeMessagesForChatTransport(messages)

    expect(sanitized).toHaveLength(1)
    expect(sanitized[0]?.parts).toEqual([
      { type: 'text', text: '此前联网搜索「大众交通 涨停」并参考了 1 个公开来源。' },
      { type: 'text', text: '结论：有公开报道。' },
    ])
    expect(JSON.stringify(sanitized)).not.toContain('tool-web_search')
    expect(JSON.stringify(sanitized)).not.toContain('providerExecuted')
  })
})
