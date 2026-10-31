import type { Env } from '../app'

export type ModelUsageEvent = {
  lane: 'chat' | 'agent_lane'
  provider: string
  model: string
  success: boolean
  durationMs: number
}

// 只记 lane/provider/model/成功与否/耗时：不记用户 ID、对话内容，和 request-observability /
// agent-lane 现有日志口径一致。写入失败不该影响主流程，吞掉即可。
export function recordModelUsage(env: Env, event: ModelUsageEvent): void {
  const dataset = env.LLM_USAGE_ANALYTICS
  if (!dataset) return
  try {
    dataset.writeDataPoint({
      indexes: [event.provider || 'unknown'],
      blobs: [event.lane, event.provider || 'unknown', event.model || 'unknown'],
      doubles: [event.success ? 1 : 0, event.durationMs],
    })
  } catch {
    // Analytics Engine 不可用时静默跳过。
  }
}
