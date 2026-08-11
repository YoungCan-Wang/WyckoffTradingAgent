export interface LlmUsageMetrics {
  inputTokens: number
  outputTokens: number
  cacheReadTokens: number
  cacheWriteTokens: number
  generationMs: number
  outputTokPerS: number | null
  cacheHitRate: number | null
  cacheReported: boolean
  segmentIndex?: number
}

export function outputTokPerS(outputTokens: number, generationMs: number): number | null {
  if (outputTokens <= 0 || generationMs <= 0) return null
  return Math.round((outputTokens / (generationMs / 1000)) * 10) / 10
}

/**
 * Cache hit % = cacheRead / input.
 * Anthropic-style raw usage can report uncached input only (cacheRead > input);
 * fold cache into the denominator in that case so rate never exceeds 100%.
 */
export function cacheHitRatePct(cacheReadTokens: number, inputTokens: number): number | null {
  const read = Math.max(0, cacheReadTokens)
  const input = Math.max(0, inputTokens)
  const denom = read > input ? input + read : input
  if (denom <= 0) return null
  return Math.min(100, Math.round((100 * read) / denom))
}

export function buildLlmUsageMetrics(input: {
  inputTokens?: number | null
  outputTokens?: number | null
  cacheReadTokens?: number | null
  cacheWriteTokens?: number | null
  generationMs?: number | null
  cacheReported?: boolean
  segmentIndex?: number
}): LlmUsageMetrics {
  const inputTokens = Math.max(0, Math.trunc(input.inputTokens || 0))
  const outputTokens = Math.max(0, Math.trunc(input.outputTokens || 0))
  const cacheReadTokens = Math.max(0, Math.trunc(input.cacheReadTokens || 0))
  const cacheWriteTokens = Math.max(0, Math.trunc(input.cacheWriteTokens || 0))
  const generationMs = Math.max(0, Math.trunc(input.generationMs || 0))
  const cacheReported = Boolean(input.cacheReported)
  return {
    inputTokens,
    outputTokens,
    cacheReadTokens,
    cacheWriteTokens,
    generationMs,
    outputTokPerS: outputTokPerS(outputTokens, generationMs),
    cacheHitRate: cacheReported ? cacheHitRatePct(cacheReadTokens, inputTokens) : null,
    cacheReported,
    segmentIndex: input.segmentIndex,
  }
}

/** Merge segment usages for a multi-continuation turn (sum tokens / gen time). */
export function mergeLlmUsageMetrics(parts: LlmUsageMetrics[]): LlmUsageMetrics | null {
  if (parts.length === 0) return null
  return buildLlmUsageMetrics({
    inputTokens: parts.reduce((sum, part) => sum + part.inputTokens, 0),
    outputTokens: parts.reduce((sum, part) => sum + part.outputTokens, 0),
    cacheReadTokens: parts.reduce((sum, part) => sum + part.cacheReadTokens, 0),
    cacheWriteTokens: parts.reduce((sum, part) => sum + part.cacheWriteTokens, 0),
    generationMs: parts.reduce((sum, part) => sum + part.generationMs, 0),
    cacheReported: parts.some((part) => part.cacheReported),
  })
}

export function formatLlmUsageLine(usage: LlmUsageMetrics): string {
  const parts: string[] = []
  if (usage.inputTokens || usage.outputTokens) {
    parts.push(`↑${usage.inputTokens.toLocaleString()} ↓${usage.outputTokens.toLocaleString()}`)
  }
  if (usage.outputTokPerS != null && usage.outputTokPerS > 0) {
    parts.push(`${usage.outputTokPerS} tok/s`)
  }
  if (usage.cacheReported && usage.cacheHitRate != null && usage.inputTokens > 0) {
    parts.push(`cache ${usage.cacheHitRate}%`)
  }
  if (usage.generationMs > 0) {
    // Model-only window (excludes tool time); label avoids confusion with wall elapsed.
    parts.push(`${(usage.generationMs / 1000).toFixed(1)}s gen`)
  }
  return parts.join(' · ')
}
