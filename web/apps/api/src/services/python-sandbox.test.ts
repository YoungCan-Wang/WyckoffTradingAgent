import { describe, expect, it, vi } from 'vitest'
import type { Env } from '../app'
import { executePythonSandbox, type SandboxBridgeFetch } from './python-sandbox'

const sandboxResult = {
  exitCode: 0,
  stdout: 'research ok\n',
  stderr: '',
  activeCpuUsageMs: 42,
  networkIngressBytes: 0,
  networkEgressBytes: 0,
}

const sandboxEnv: Env = {
  SANDBOX_BRIDGE_URL: 'https://sandbox-bridge.example.com/api/sandbox-run',
  SANDBOX_BRIDGE_SECRET: 'test-bridge-secret',
  AGENT_SANDBOX_TIMEOUT_MS: '45000',
}

describe('Python sandbox bridge client', () => {
  it('signs a bounded request and returns the bridge result', async () => {
    const bridgeFetch: SandboxBridgeFetch = vi.fn(async () => Response.json(sandboxResult))
    const result = await executePythonSandbox(sandboxEnv, 'print("ok")', {
      requestId: 'request-1',
      runId: 'run-1',
    }, bridgeFetch)

    expect(result).toEqual(sandboxResult)
    expect(bridgeFetch).toHaveBeenCalledOnce()
    const [url, init] = vi.mocked(bridgeFetch).mock.calls[0]
    expect(url).toBe(sandboxEnv.SANDBOX_BRIDGE_URL)
    expect(init.method).toBe('POST')
    expect(init.body).toBe(JSON.stringify({ script: 'print("ok")', timeout: 45_000 }))
    const headers = new Headers(init.headers)
    expect(headers.get('x-wyckoff-timestamp')).toMatch(/^\d{13}$/)
    expect(headers.get('x-wyckoff-signature')).toMatch(/^[a-f0-9]{64}$/)
    expect(headers.get('x-wyckoff-request-id')).toBe('request-1')
    expect(headers.get('x-wyckoff-run-id')).toBe('run-1')
  })

  it('fails closed for incomplete config and rejected bridge calls', async () => {
    await expect(executePythonSandbox({} as Env, 'print(1)')).rejects.toThrow('Sandbox bridge configuration is incomplete')
    const bridgeFetch: SandboxBridgeFetch = vi.fn(async () => new Response(null, { status: 401 }))
    await expect(executePythonSandbox(sandboxEnv, 'print(1)', { runId: 'run-1' }, bridgeFetch)).rejects.toThrow('Sandbox bridge request failed')
  })
})
