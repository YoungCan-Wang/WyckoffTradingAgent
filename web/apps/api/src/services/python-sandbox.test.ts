import { describe, expect, it, vi } from 'vitest'
import type { Env } from '../app'
import { executePythonSandbox, type SandboxHandle } from './python-sandbox'

function sandbox(overrides: Partial<SandboxHandle> = {}): SandboxHandle {
  return {
    writeFiles: vi.fn(async () => undefined),
    runCommand: vi.fn(async () => ({
      exitCode: 0,
      stdout: async () => 'research ok\n',
      stderr: async () => '',
    })),
    stop: vi.fn(async () => ({
      activeCpuUsageMs: 42,
      networkTransfer: { ingress: 0, egress: 0 },
    })),
    delete: vi.fn(async () => undefined),
    ...overrides,
  }
}

describe('Python sandbox executor', () => {
  it('runs a script without secrets and permanently deletes the sandbox', async () => {
    const handle = sandbox()
    const factory = vi.fn(async () => handle)
    const result = await executePythonSandbox({ AGENT_SANDBOX_TIMEOUT_MS: '45000' }, 'print("ok")', factory)

    expect(factory).toHaveBeenCalledWith(expect.any(Object), {
      runtime: 'python3.13',
      timeout: 45_000,
      networkPolicy: 'deny-all',
      persistent: false,
      tags: { app: 'wyckoff', kind: 'python-research' },
    })
    expect(handle.writeFiles).toHaveBeenCalledWith([{ path: 'main.py', content: 'print("ok")' }])
    expect(handle.runCommand).toHaveBeenCalledWith('sh', ['-c', expect.stringContaining('ulimit -f 64')])
    expect(handle.stop).toHaveBeenCalledOnce()
    expect(handle.delete).toHaveBeenCalledOnce()
    expect(result).toMatchObject({ exitCode: 0, stdout: 'research ok\n', activeCpuUsageMs: 42 })
  })

  it('deletes the sandbox when execution fails', async () => {
    const handle = sandbox({ writeFiles: vi.fn(async () => { throw new Error('write failed') }) })
    await expect(executePythonSandbox({} as Env, 'print(1)', async () => handle)).rejects.toThrow('write failed')
    expect(handle.stop).not.toHaveBeenCalled()
    expect(handle.delete).toHaveBeenCalledOnce()
  })

  it('caps command output returned to the API', async () => {
    const handle = sandbox({
      runCommand: vi.fn(async () => ({
        exitCode: 0,
        stdout: async () => 'x'.repeat(40_000),
        stderr: async () => '',
      })),
    })
    const result = await executePythonSandbox({} as Env, 'print(1)', async () => handle)
    expect(result.stdout.length).toBeLessThan(40_000)
    expect(result.stdout).toContain('...[truncated]')
  })
})
