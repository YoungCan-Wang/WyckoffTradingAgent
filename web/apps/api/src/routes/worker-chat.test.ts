import { describe, expect, it, vi } from 'vitest'
import { isActivePlanetMember } from '../middleware/planet-membership'
import { buildSandboxTools } from './worker-chat'

vi.mock('../middleware/planet-membership', () => ({ isActivePlanetMember: vi.fn() }))
vi.mock('./chat', () => ({
  createChatRoutes: vi.fn(() => ({})),
  createUserSupabase: vi.fn(() => ({})),
}))

const membership = vi.mocked(isActivePlanetMember)

describe('sandbox tool registration', () => {
  it('registers no tool when the sandbox is disabled', async () => {
    await expect(buildSandboxTools({}, 'user-1', 'token', 'request-1')).resolves.toEqual({})
    expect(membership).not.toHaveBeenCalled()
  })

  it('hides the tool from non-members', async () => {
    membership.mockResolvedValueOnce(false)
    const tools = await buildSandboxTools({ AGENT_SANDBOX_ENABLED: 'true' }, 'user-1', 'token', 'request-1')
    expect(tools).toEqual({})
  })

  it('exposes run_python_research to planet members', async () => {
    membership.mockResolvedValueOnce(true)
    const tools = await buildSandboxTools({ AGENT_SANDBOX_ENABLED: 'true' }, 'user-1', 'token', 'request-1')
    expect(Object.keys(tools)).toEqual(['run_python_research'])
  })
})
