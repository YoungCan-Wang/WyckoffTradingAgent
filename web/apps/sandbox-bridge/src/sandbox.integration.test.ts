import { describe, expect, it } from 'vitest'
import { executePythonSandbox } from './sandbox.js'

const describeSandbox = process.env.RUN_VERCEL_SANDBOX_INTEGRATION === '1' ? describe : describe.skip

describeSandbox('Vercel Sandbox integration', () => {
  it('executes Python with denied network access and deletes the microVM', async () => {
    const result = await executePythonSandbox('print("wyckoff-sandbox-ok")', 60_000)
    expect(result.exitCode).toBe(0)
    expect(result.stdout.trim()).toBe('wyckoff-sandbox-ok')
    expect(result.networkEgressBytes).toBeGreaterThanOrEqual(0)
  }, 120_000)
})
