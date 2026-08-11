import { afterEach, describe, expect, it, vi } from 'vitest'
import { STREAM_UI_THROTTLE_MS, clearStreamFlush, scheduleStreamFlush } from '../stream-render'

describe('scheduleStreamFlush', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('coalesces multiple deltas into one set within the throttle window', () => {
    vi.useFakeTimers()
    const buf = { current: '' }
    const timer = { current: 0 as ReturnType<typeof setTimeout> | 0 }
    const set = vi.fn()

    buf.current = 'a'
    scheduleStreamFlush(buf, timer, set)
    buf.current = 'ab'
    scheduleStreamFlush(buf, timer, set)
    buf.current = 'abc'
    scheduleStreamFlush(buf, timer, set)

    expect(set).not.toHaveBeenCalled()
    vi.advanceTimersByTime(STREAM_UI_THROTTLE_MS)
    expect(set).toHaveBeenCalledTimes(1)
    expect(set).toHaveBeenCalledWith('abc')
  })

  it('clearStreamFlush cancels a pending update', () => {
    vi.useFakeTimers()
    const buf = { current: 'x' }
    const timer = { current: 0 as ReturnType<typeof setTimeout> | 0 }
    const set = vi.fn()

    scheduleStreamFlush(buf, timer, set)
    clearStreamFlush(timer)
    vi.advanceTimersByTime(STREAM_UI_THROTTLE_MS)
    expect(set).not.toHaveBeenCalled()
  })
})
