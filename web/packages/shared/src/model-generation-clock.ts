/** Chunk types that mean the model is actively producing tokens. */
const MODEL_CONTENT_TYPES = new Set([
  'text-delta',
  'reasoning-delta',
  'text-start',
  'reasoning-start',
])

/** Chunk types that pause the model-only generation clock (tools / step boundaries). */
const TOOL_BOUNDARY_TYPES = new Set([
  'tool-input-start',
  'tool-input-available',
  'tool-input-error',
  'tool-approval-request',
  'finish-step',
])

export function isModelContentChunkType(type: string): boolean {
  return MODEL_CONTENT_TYPES.has(type)
}

export function isToolBoundaryChunkType(type: string): boolean {
  return TOOL_BOUNDARY_TYPES.has(type)
}

/**
 * Accumulate model-only generation windows for tok/s.
 * Time between tool boundaries is excluded; finalize falls back to full wall
 * time only when no content window was ever opened.
 */
export function createModelGenerationClock(
  streamStartedAt: number,
  now: () => number = Date.now,
) {
  let generationMs = 0
  let windowStart: number | null = null

  const closeWindow = (at = now()) => {
    if (windowStart == null) return
    generationMs += Math.max(0, at - windowStart)
    windowStart = null
  }

  return {
    onChunkType(type: string) {
      if (isModelContentChunkType(type)) {
        windowStart ??= now()
        return
      }
      if (isToolBoundaryChunkType(type)) closeWindow()
    },
    finalize() {
      const endedAt = now()
      closeWindow(endedAt)
      if (generationMs > 0) return generationMs
      return Math.max(0, endedAt - streamStartedAt)
    },
  }
}
