import { describe, expect, it, vi } from 'vitest'
import { isActiveWhitelistUser } from '../middleware/whitelist'
import { buildSandboxTools } from './worker-chat'

vi.mock('../middleware/whitelist', () => ({ isActiveWhitelistUser: vi.fn() }))
vi.mock('./chat', () => ({
  createChatRoutes: vi.fn(() => ({})),
  createUserSupabase: vi.fn(() => ({})),
}))

const whitelist = vi.mocked(isActiveWhitelistUser)

describe('sandbox tool registration', () => {
  it('registers no tool when the sandbox is disabled', async () => {
    await expect(buildSandboxTools({}, 'user-1', 'token', 'request-1')).resolves.toEqual({})
    expect(whitelist).not.toHaveBeenCalled()
  })

  it('hides the tool from non-whitelisted users', async () => {
    whitelist.mockResolvedValueOnce(false)
    const tools = await buildSandboxTools({ AGENT_SANDBOX_ENABLED: 'true' }, 'user-1', 'token', 'request-1')
    expect(tools).toEqual({})
  })

  it('exposes run_python_research to whitelisted users', async () => {
    whitelist.mockResolvedValueOnce(true)
    const tools = await buildSandboxTools({ AGENT_SANDBOX_ENABLED: 'true' }, 'user-1', 'token', 'request-1')
    expect(Object.keys(tools)).toEqual(['run_python_research'])
  })
})
