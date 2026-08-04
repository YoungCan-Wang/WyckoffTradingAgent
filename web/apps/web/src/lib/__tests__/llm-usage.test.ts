import { describe, expect, it } from 'vitest'
import {
  buildLlmUsageMetrics,
  cacheHitRatePct,
  createModelGenerationClock,
  formatLlmUsageLine,
  mergeLlmUsageMetrics,
} from '@wyckoff/shared'

describe('llm-usage', () => {
  it('computes tok/s and cache hit rate', () => {
    const usage = buildLlmUsageMetrics({
      inputTokens: 100,
      outputTokens: 50,
      cacheReadTokens: 80,
      cacheWriteTokens: 0,
      generationMs: 250,
      cacheReported: true,
    })
    expect(usage.outputTokPerS).toBe(200)
    expect(usage.cacheHitRate).toBe(80)
    expect(formatLlmUsageLine(usage)).toBe('↑100 ↓50 · 200 tok/s · cache 80% · 0.3s gen')
  })

  it('folds anthropic-style uncached input into cache denominator', () => {
    expect(cacheHitRatePct(800, 200)).toBe(80)
    expect(cacheHitRatePct(800, 1000)).toBe(80)
  })

  it('merges multi-segment usages', () => {
    const merged = mergeLlmUsageMetrics([
      buildLlmUsageMetrics({
        inputTokens: 10,
        outputTokens: 20,
        cacheReadTokens: 0,
        generationMs: 100,
        cacheReported: true,
        segmentIndex: 0,
      }),
      buildLlmUsageMetrics({
        inputTokens: 30,
        outputTokens: 40,
        cacheReadTokens: 15,
        generationMs: 100,
        cacheReported: true,
        segmentIndex: 1,
      }),
    ])
    expect(merged).toMatchObject({
      inputTokens: 40,
      outputTokens: 60,
      cacheReadTokens: 15,
      generationMs: 200,
      outputTokPerS: 300,
      cacheHitRate: 38,
    })
  })

  it('hides cache when not reported', () => {
    const usage = buildLlmUsageMetrics({
      inputTokens: 10,
      outputTokens: 5,
      cacheReadTokens: 0,
      generationMs: 100,
      cacheReported: false,
    })
    expect(usage.cacheHitRate).toBeNull()
    expect(formatLlmUsageLine(usage)).toBe('↑10 ↓5 · 50 tok/s · 0.1s gen')
  })
})

describe('model-generation-clock', () => {
  it('excludes tool gaps from generationMs', () => {
    let now = 1_000
    const clock = createModelGenerationClock(now, () => now)
    clock.onChunkType('text-start')
    now = 1_100
    clock.onChunkType('text-delta')
    now = 1_200
    clock.onChunkType('tool-input-start') // close 200ms window
    now = 5_000 // tools running
    clock.onChunkType('text-delta')
    now = 5_050
    clock.onChunkType('finish-step')
    expect(clock.finalize()).toBe(250)
  })

  it('falls back to wall time when no model content', () => {
    let now = 0
    const clock = createModelGenerationClock(0, () => now)
    clock.onChunkType('tool-input-start')
    now = 800
    expect(clock.finalize()).toBe(800)
  })
})
