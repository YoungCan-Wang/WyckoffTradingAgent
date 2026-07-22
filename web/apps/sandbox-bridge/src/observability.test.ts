import { describe, expect, it, vi } from 'vitest'
import { bridgeLogContext, logSandboxBridge } from './observability.js'

describe('sandbox bridge observability', () => {
  it('retains only safe correlation identifiers from request headers', () => {
    expect(bridgeLogContext({
      'x-wyckoff-request-id': 'request-1',
      'x-wyckoff-run-id': 'run-1',
    })).toEqual({ requestId: 'request-1', runId: 'run-1' })
    expect(bridgeLogContext({
      'x-wyckoff-request-id': 'request with spaces',
      'x-wyckoff-run-id': 'run-1\nscript=secret',
    })).toEqual({ requestId: undefined, runId: undefined })
  })

  it('writes execution metadata without a script payload', () => {
    const write = vi.spyOn(console, 'info').mockImplementation(() => undefined)
    logSandboxBridge('finished', {
      requestId: 'request-1',
      runId: 'run-1',
      durationMs: 25,
      exitCode: 0,
      status: 'completed',
      usage: { activeCpuUsageMs: 10, networkIngressBytes: 0, networkEgressBytes: 0 },
    })

    expect(JSON.parse(write.mock.calls[0]?.[0] || '{}')).toMatchObject({
      event: 'sandbox_bridge.finished',
      requestId: 'request-1',
      runId: 'run-1',
      durationMs: 25,
      status: 'completed',
    })
    write.mockRestore()
  })
})
