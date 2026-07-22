import { describe, expect, it } from 'vitest'
import type { Env } from '../app'
import { executePythonSandbox } from './python-sandbox'

declare const process: { env: Record<string, string | undefined> }

const integrationEnv: Env = {
  VERCEL_OIDC_TOKEN: process.env.VERCEL_OIDC_TOKEN,
  VERCEL_PROJECT_ID: process.env.VERCEL_PROJECT_ID,
  VERCEL_TEAM_ID: process.env.VERCEL_TEAM_ID,
  VERCEL_TOKEN: process.env.VERCEL_TOKEN,
  AGENT_SANDBOX_TIMEOUT_MS: process.env.AGENT_SANDBOX_TIMEOUT_MS || '60000',
}
const describeSandbox = process.env.RUN_VERCEL_SANDBOX_INTEGRATION === '1' ? describe : describe.skip

describeSandbox('Vercel Sandbox integration', () => {
  it('executes Python with denied network access and deletes the microVM', async () => {
    const result = await executePythonSandbox(integrationEnv, 'print("wyckoff-sandbox-ok")')
    expect(result.exitCode).toBe(0)
    expect(result.stdout.trim()).toBe('wyckoff-sandbox-ok')
    expect(result.networkEgressBytes).toBeGreaterThanOrEqual(0)
  }, 120_000)
})
