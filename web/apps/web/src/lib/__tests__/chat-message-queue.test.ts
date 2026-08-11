import { describe, expect, it } from 'vitest'
import type { UIMessage } from 'ai'
import { hasPendingToolApproval } from '@/features/reading-room/chat-state'

describe('hasPendingToolApproval', () => {
  it('detects approval-requested tool parts', () => {
    const messages = [
      {
        id: 'm1',
        role: 'assistant',
        parts: [{ type: 'tool-execute_portfolio_update', state: 'approval-requested', toolCallId: 't1' }],
      },
    ] as unknown as UIMessage[]
    expect(hasPendingToolApproval(messages)).toBe(true)
  })

  it('returns false when no approval is pending', () => {
    const messages = [
      {
        id: 'm1',
        role: 'assistant',
        parts: [{ type: 'tool-market_overview', state: 'output-available', toolCallId: 't1' }],
      },
    ] as unknown as UIMessage[]
    expect(hasPendingToolApproval(messages)).toBe(false)
  })
})
