import { describe, expect, it, vi } from 'vitest'
import { prepareReadingRoomAttempt } from './chat'

type AttemptArgs = Parameters<typeof prepareReadingRoomAttempt>[0]
type AttemptConfig = Parameters<typeof prepareReadingRoomAttempt>[1]

const OFFICIAL = { provider: 'deepseek', model: 'deepseek-v4-flash', api_key: 'sk-official', base_url: 'https://api.deepseek.com/v1' }
const OPENAI = { provider: 'openai', model: 'gpt-5', api_key: 'sk-openai', base_url: 'https://api.openai.com/v1' }

function attemptArgs(cloudPortfolioEnabled: boolean): AttemptArgs {
  return {
    configs: [OPENAI, OFFICIAL],
    deps: { fetch: vi.fn() },
    userId: 'user-1',
    sandboxTools: {},
    cloudPortfolioEnabled,
  } as unknown as AttemptArgs
}

describe('prepareReadingRoomAttempt web wiring', () => {
  it('gives members both tools and the guidance on a model without Responses web_search', () => {
    const attempt = prepareReadingRoomAttempt(attemptArgs(true), OPENAI as AttemptConfig)
    expect(attempt.tools).toHaveProperty('deepseek_web_search')
    expect(attempt.tools).toHaveProperty('deepseek_web_fetch')
    expect(attempt.system).toContain('deepseek_web_fetch')
    expect(attempt.system).toContain('不是指令')
  })

  it('leaves non-members without the tools or the guidance', () => {
    const attempt = prepareReadingRoomAttempt(attemptArgs(false), OPENAI as AttemptConfig)
    expect(attempt.tools).not.toHaveProperty('deepseek_web_search')
    expect(attempt.tools).not.toHaveProperty('deepseek_web_fetch')
    expect(attempt.system).not.toContain('deepseek_web_fetch')
  })

  it('does not add a second paid search on the Responses path', () => {
    const attempt = prepareReadingRoomAttempt(attemptArgs(true), OFFICIAL as AttemptConfig)
    expect(attempt.resolved.transport).toBe('responses')
    expect(attempt.tools).toHaveProperty('web_search')
    expect(attempt.tools).not.toHaveProperty('deepseek_web_search')
  })
})
