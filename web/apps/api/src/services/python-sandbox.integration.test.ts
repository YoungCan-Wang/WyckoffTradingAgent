import { env } from 'cloudflare:workers'
import { describe, expect, it } from 'vitest'
import type { Env } from '../app'
import { executePythonSandbox } from './python-sandbox'

const integrationEnv = env as Env
const describeSandbox = integrationEnv.RUN_VERCEL_SANDBOX_INTEGRATION === '1' ? describe : describe.skip

describeSandbox('Vercel Sandbox integration', () => {
  it('executes Python with denied network access and deletes the microVM', async () => {
    const result = await executePythonSandbox(integrationEnv, 'print("wyckoff-sandbox-ok")')
    expect(result.exitCode).toBe(0)
    expect(result.stdout.trim()).toBe('wyckoff-sandbox-ok')
    expect(result.networkEgressBytes).toBeGreaterThanOrEqual(0)
  }, 120_000)
})
