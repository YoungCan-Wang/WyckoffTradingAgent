import { Chat } from '@ai-sdk/react'
import type { UIMessage } from 'ai'
import { describe, expect, it } from 'vitest'

const pendingApprovalMessage = {
  id: 'assistant-1',
  role: 'assistant',
  parts: [{
    type: 'dynamic-tool',
    toolCallId: 'tool-call-1',
    toolName: 'run_python_research',
    input: { purpose: '验证求和', script: 'print(sum(range(101)))' },
    state: 'approval-requested',
    approval: { id: 'approval-1', signature: 'signed-approval' },
  }],
} as UIMessage

describe('chat tool approvals', () => {
  it('keeps the server-issued signature after approval', async () => {
    const chat = new Chat<UIMessage>({ messages: [pendingApprovalMessage] })

    await chat.addToolApprovalResponse({ id: 'approval-1', approved: true })

    expect(chat.messages[0]?.parts[0]).toMatchObject({
      state: 'approval-responded',
      approval: { id: 'approval-1', approved: true, signature: 'signed-approval' },
    })
  })
})
