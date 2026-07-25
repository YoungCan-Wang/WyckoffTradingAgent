import type { UIMessage } from 'ai'
import { describe, expect, it } from 'vitest'
import { replaceConversationToolOutput } from './conversations'

const queuedOutput = {
  id: 'run-1',
  kind: 'python_research',
  status: 'queued',
  attempts: 0,
  createdAt: '2026-07-25T00:00:00.000Z',
}

const completedOutput = {
  ...queuedOutput,
  status: 'completed',
  finishedAt: '2026-07-25T00:00:01.000Z',
  exitCode: 0,
  stdout: '5050\n',
}

const messages = [{
  id: 'assistant-tool',
  role: 'assistant',
  parts: [{
    type: 'tool-run_python_research',
    toolCallId: 'tool-1',
    state: 'output-available',
    output: queuedOutput,
  }],
}, {
  id: 'assistant-text',
  role: 'assistant',
  parts: [{ type: 'text', text: '任务已排队。' }],
}] as UIMessage[]

describe('conversation sandbox result recovery', () => {
  it('replaces only the original tool result in a stored conversation', () => {
    const updated = replaceConversationToolOutput(messages, 'tool-1', completedOutput)

    expect(updated).not.toBe(messages)
    expect(updated[0]?.parts[0]).toMatchObject({ state: 'output-available', output: completedOutput })
    expect(updated[1]).toBe(messages[1])
    expect(messages[0]?.parts[0]).toMatchObject({ output: queuedOutput })
  })

  it('keeps the saved conversation unchanged when the tool call is absent', () => {
    expect(replaceConversationToolOutput(messages, 'missing-tool', completedOutput)).toBe(messages)
  })
})
