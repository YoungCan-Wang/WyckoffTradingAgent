import type { UIMessage } from 'ai'
import { describe, expect, it } from 'vitest'
import { preferStoredTerminalToolOutputs, replaceConversationToolOutput } from './conversations'

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

  it('keeps stored terminal sandbox output when live chat still has the queued placeholder', () => {
    const stored = replaceConversationToolOutput(messages, 'tool-1', completedOutput)
    const merged = preferStoredTerminalToolOutputs(messages, stored)

    expect(merged[0]?.parts[0]).toMatchObject({ state: 'output-available', output: completedOutput })
    expect(merged[1]).toBe(messages[1])
  })

  it('does not overwrite a live terminal sandbox output with an older stored one', () => {
    const live = replaceConversationToolOutput(messages, 'tool-1', completedOutput)
    const storedFailed = replaceConversationToolOutput(messages, 'tool-1', {
      ...queuedOutput,
      status: 'failed',
      error: 'old',
    })

    expect(preferStoredTerminalToolOutputs(live, storedFailed)).toBe(live)
  })

  it('collapses approval and output copies of the same sandbox tool call', () => {
    const duplicated = [{
      id: 'assistant-approval',
      role: 'assistant',
      parts: [{
        type: 'tool-run_python_research', toolCallId: 'tool-1', state: 'approval-responded',
        approval: { id: 'approval-1', approved: true, signature: 'signed' },
      }],
    }, ...messages] as UIMessage[]

    const updated = replaceConversationToolOutput(duplicated, 'tool-1', completedOutput)

    expect(updated).toHaveLength(messages.length)
    expect(updated.some((message) => message.id === 'assistant-approval')).toBe(false)
    expect(updated[0]?.parts[0]).toMatchObject({ state: 'output-available', output: completedOutput })
    expect(replaceConversationToolOutput(updated, 'tool-1', completedOutput)).toBe(updated)
  })

  it('preserves an additional output copy instead of silently repairing it', () => {
    const duplicated = [messages[0]!, messages[0]!, messages[1]!]

    const updated = replaceConversationToolOutput(duplicated, 'tool-1', completedOutput)

    expect(updated).toHaveLength(duplicated.length)
    expect(updated[0]?.parts[0]).toMatchObject({ output: queuedOutput })
    expect(updated[1]?.parts[0]).toMatchObject({ output: completedOutput })
  })
})
