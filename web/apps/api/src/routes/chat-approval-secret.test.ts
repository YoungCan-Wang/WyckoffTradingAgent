import { describe, expect, it } from 'vitest'
import type { Env } from '../app'
import { getToolApprovalSecret } from './chat'

describe('tool approval secret', () => {
  it('returns the dedicated secret when set', async () => {
    const env = { CHAT_TOOL_APPROVAL_SECRET: 'dedicated-secret' } as Env
    await expect(getToolApprovalSecret(env)).resolves.toBe('dedicated-secret')
  })

  it('derives a domain-separated rollout key without returning the service role value', async () => {
    const env = { SUPABASE_SERVICE_ROLE_KEY: 'service-role-key' } as Env
    const derived = await getToolApprovalSecret(env)
    expect(derived).toMatch(/^[a-f0-9]{64}$/)
    expect(derived).not.toContain('service-role-key')
  })

  it('throws when nothing is configured', async () => {
    await expect(getToolApprovalSecret({} as Env)).rejects.toThrow('Missing CHAT_TOOL_APPROVAL_SECRET')
  })

  it('rejects an empty secret rather than signing with it', async () => {
    const env = { CHAT_TOOL_APPROVAL_SECRET: '' } as Env
    await expect(getToolApprovalSecret(env)).rejects.toThrow('Missing CHAT_TOOL_APPROVAL_SECRET')
  })
})
