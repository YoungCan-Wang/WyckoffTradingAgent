import { describe, expect, it } from 'vitest'
import type { Env } from '../app'
import { getToolApprovalSecret, missingWorkerRuntimeSecrets } from './runtime-readiness'

describe('Worker runtime readiness', () => {
  it('uses a dedicated approval secret with sufficient entropy', () => {
    const secret = 'a'.repeat(64)
    expect(getToolApprovalSecret({ CHAT_TOOL_APPROVAL_SECRET: secret } as Env)).toBe(secret)
    expect(missingWorkerRuntimeSecrets({ CHAT_TOOL_APPROVAL_SECRET: secret } as Env)).toEqual([])
  })

  it('does not accept the Supabase service role as an approval secret', () => {
    const env = { SUPABASE_SERVICE_ROLE_KEY: 'service-role-key' } as Env
    expect(() => getToolApprovalSecret(env)).toThrow('读盘室服务配置异常')
    expect(missingWorkerRuntimeSecrets(env)).toEqual(['CHAT_TOOL_APPROVAL_SECRET'])
  })

  it.each([undefined, '', 'too-short'])('rejects a missing or weak approval secret', (secret) => {
    expect(() => getToolApprovalSecret({ CHAT_TOOL_APPROVAL_SECRET: secret } as Env)).toThrow(
      '读盘室服务配置异常',
    )
  })
})
