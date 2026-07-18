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
