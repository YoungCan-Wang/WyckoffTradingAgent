/** UI refresh coalescing for LLM token streams (ms). */
export const STREAM_UI_THROTTLE_MS = 120

type TimerHandle = ReturnType<typeof setTimeout>

/** Batch token deltas into one state update per throttle window. */
export function scheduleStreamFlush(
  buf: { current: string },
  timer: { current: TimerHandle | 0 },
  set: (value: string) => void,
  minMs = STREAM_UI_THROTTLE_MS,
): void {
  if (timer.current) return
  timer.current = setTimeout(() => {
    timer.current = 0
    set(buf.current)
  }, minMs)
}

export function clearStreamFlush(timer: { current: TimerHandle | 0 }): void {
  if (!timer.current) return
  clearTimeout(timer.current)
  timer.current = 0
}
