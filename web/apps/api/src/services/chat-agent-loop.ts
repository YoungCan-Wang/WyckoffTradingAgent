export const CHAT_MAX_OUTPUT_TOKENS = 8192
export const CHAT_MAX_STEPS = 16
export const CHAT_MAX_TOTAL_STEPS = 32
export const CHAT_MAX_AUTO_CONTINUATIONS = 2

export const CHAT_CONTINUATION_PROMPT = '继续完成当前分析。复用已经完成的工具结果，不要重复已完成的数据读取；如果关键数据已经足够，直接补全并给出完整结论。'

export type AgentContinuationReason = 'output-length' | 'step-limit'

export type AgentLoopDecision =
  | { kind: 'complete' }
  | { kind: 'continue'; reason: AgentContinuationReason }
  | { kind: 'error'; message: string }

export function decideAgentLoop(input: {
  finishReason: string
  stepCount: number
  maxSteps: number
  hasToolCalls: boolean
  hasToolApproval: boolean
  hasIncompleteToolCall: boolean
}): AgentLoopDecision {
  if (input.hasToolApproval) return { kind: 'complete' }

  if (input.hasIncompleteToolCall) {
    return {
      kind: 'error',
      message: '模型在工具参数尚未完整生成时中断，本轮无法安全续跑。请点击“继续本轮”重新补齐缺失步骤。',
    }
  }

  if (input.finishReason === 'length') return { kind: 'continue', reason: 'output-length' }
  if (input.hasToolCalls && input.stepCount >= input.maxSteps) return { kind: 'continue', reason: 'step-limit' }

  return { kind: 'complete' }
}

export function continuationLimitMessage(reason: AgentContinuationReason): string {
  return reason === 'output-length'
    ? `模型输出达到单次 ${CHAT_MAX_OUTPUT_TOKENS} 个 token 上限，自动续写次数已用尽。请点击“继续本轮”完成剩余分析。`
    : `本轮工具执行达到 ${CHAT_MAX_TOTAL_STEPS} 步上限，自动续跑次数已用尽。请点击“继续本轮”完成剩余分析。`
}
