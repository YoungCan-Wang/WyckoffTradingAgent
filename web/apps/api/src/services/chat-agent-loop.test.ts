import { convertToModelMessages, type UIMessage } from 'ai'
import { removeSupersededToolApprovals } from '@wyckoff/shared'
import { describe, expect, it } from 'vitest'
import { decideAgentLoop } from './chat-agent-loop'

const baseInput = {
  finishReason: 'stop',
  stepCount: 1,
  maxSteps: 16,
  hasToolCalls: false,
  hasToolApproval: false,
  hasIncompleteToolCall: false,
}

describe('chat agent loop decisions', () => {
  it('continues a complete answer that hit the model output limit', () => {
    expect(decideAgentLoop({ ...baseInput, finishReason: 'length' })).toEqual({
      kind: 'continue',
      reason: 'output-length',
    })
  })

  it('continues after a tool loop reaches its segment limit', () => {
    expect(decideAgentLoop({ ...baseInput, finishReason: 'tool-calls', stepCount: 16, hasToolCalls: true })).toEqual({
      kind: 'continue',
      reason: 'step-limit',
    })
  })

  it('does not continue a pending user approval', () => {
    expect(decideAgentLoop({ ...baseInput, finishReason: 'tool-calls', stepCount: 16, hasToolCalls: true, hasToolApproval: true })).toEqual({ kind: 'complete' })
  })

  it('does not replay an incomplete tool call automatically', () => {
    expect(decideAgentLoop({ ...baseInput, finishReason: 'length', hasIncompleteToolCall: true })).toEqual({
      kind: 'error',
      message: '模型在工具参数尚未完整生成时中断，本轮无法安全续跑。请点击“继续本轮”重新补齐缺失步骤。',
    })
  })
})

describe('settled tool message normalization', () => {
  it('removes the superseded approval message before model conversion', async () => {
    const normalized = removeSupersededToolApprovals(duplicateSandboxLifecycle())

    expect(normalized).toHaveLength(3)
    const modelMessages = await convertToModelMessages(normalized, { ignoreIncompleteToolCalls: true })
    expect(JSON.stringify(modelMessages).match(/"toolCallId":"tool-1"/g)).toHaveLength(2)
    expect(JSON.stringify(modelMessages)).not.toContain('tool-approval-response')
  })

  it('keeps an approval response while no tool output exists', () => {
    const pending = duplicateSandboxLifecycle().slice(0, 2)

    expect(removeSupersededToolApprovals(pending)).toBe(pending)
  })

  it('does not rewrite duplicate outputs or an approval that follows an output', () => {
    const lifecycle = duplicateSandboxLifecycle()
    const output = lifecycle[2]!
    const unusual = [lifecycle[0]!, output, output, lifecycle[1]!, lifecycle[3]!]

    expect(removeSupersededToolApprovals(unusual)).toBe(unusual)
  })
})

function duplicateSandboxLifecycle(): UIMessage[] {
  const input = { purpose: '验证求和', script: 'print(sum(range(101)))' }
  return [{
    id: 'user-1',
    role: 'user',
    parts: [{ type: 'text', text: '计算一下' }],
  }, {
    id: 'assistant-approval',
    role: 'assistant',
    parts: [{
      type: 'dynamic-tool', toolCallId: 'tool-1', toolName: 'run_python_research', input,
      state: 'approval-responded', approval: { id: 'approval-1', approved: true, signature: 'signed' },
    }],
  }, {
    id: 'assistant-output',
    role: 'assistant',
    parts: [{
      type: 'dynamic-tool', toolCallId: 'tool-1', toolName: 'run_python_research', input,
      state: 'output-available', output: { id: 'run-1', status: 'completed', stdout: '5050\n' },
    }],
  }, {
    id: 'user-2',
    role: 'user',
    parts: [{ type: 'text', text: '解读结果' }],
  }] as UIMessage[]
}
