import { afterEach, describe, expect, it, vi } from 'vitest'
import { buildAgentServiceTools, REMOTE_TOOLS, type AgentToolsDeps } from './agent-service-tools'

const ENV = {
  AGENT_SERVICE_URL: 'https://agent.example',
  AGENT_SERVICE_TOKEN: 'service-secret',
  AGENT_TOOLS_USERS: 'user-1',
}

function deps(overrides: Partial<AgentToolsDeps> = {}): AgentToolsDeps {
  return {
    isMember: async () => true,
    loadCredentials: async () => ({ tickflow_api_key: 'tf-key', tushare_token: 'ts-token' }),
    fetchImpl: vi.fn(async () => Response.json({ result: { ok: true } })) as unknown as typeof fetch,
    ...overrides,
  }
}

async function run(tools: Awaited<ReturnType<typeof buildAgentServiceTools>>, name: string, input: unknown) {
  const execute = (tools[name] as unknown as { execute: (input: unknown, options: unknown) => Promise<unknown> }).execute
  return execute(input, { toolCallId: 't1', messages: [] })
}

afterEach(() => vi.restoreAllMocks())

describe('buildAgentServiceTools gates', () => {
  it('adds nothing when the service is not configured', async () => {
    expect(await buildAgentServiceTools({ AGENT_TOOLS_USERS: 'user-1' }, 'user-1', deps())).toEqual({})
  })

  it('adds nothing for users outside the list, even members', async () => {
    const isMember = vi.fn(async () => true)
    expect(await buildAgentServiceTools({ ...ENV, AGENT_TOOLS_USERS: 'someone-else' }, 'user-1', deps({ isMember }))).toEqual({})
    expect(isMember).not.toHaveBeenCalled()
  })

  it('adds nothing for listed users who are not active members', async () => {
    expect(await buildAgentServiceTools(ENV, 'user-1', deps({ isMember: async () => false }))).toEqual({})
  })

  it('is independent from the agent-lane list', async () => {
    const env = { ...ENV, AGENT_TOOLS_USERS: undefined, AGENT_LANE_USERS: '*' }
    expect(await buildAgentServiceTools(env, 'user-1', deps())).toEqual({})
  })

  it('exposes exactly the declared tools, none of which collide with reading-room tool names', async () => {
    const tools = await buildAgentServiceTools(ENV, 'user-1', deps())
    expect(Object.keys(tools).sort()).toEqual(REMOTE_TOOLS.map((spec) => spec.name).sort())
    const readingRoom = [
      'search_stock', 'view_portfolio', 'market_overview', 'market_history', 'stock_news', 'query_recommendations',
      'query_attribution', 'analyze_stock', 'screen_stocks', 'generate_ai_report', 'generate_strategy_decision',
      'intraday_analysis', 'plan_portfolio_update', 'execute_portfolio_update', 'run_python_research', 'web_search',
    ]
    expect(Object.keys(tools).filter((name) => readingRoom.includes(name))).toEqual([])
  })

  it('does not include tools that must stay out of a shared server', () => {
    const forbidden = ['exec_command', 'read_file', 'write_file', 'browser_research', 'run_backtest', 'screen_stocks']
    expect(REMOTE_TOOLS.map((spec) => spec.name).filter((name) => forbidden.includes(name))).toEqual([])
  })
})

describe('calling a remote tool', () => {
  it('posts to the service with the gateway credentials and returns the result', async () => {
    const fetchImpl = vi.fn(async () => Response.json({ result: { regime: 'RISK_OFF' } }))
    const tools = await buildAgentServiceTools(ENV, 'user-1', deps({ fetchImpl: fetchImpl as unknown as typeof fetch }))

    expect(await run(tools, 'market_regime', {})).toEqual({ regime: 'RISK_OFF' })

    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('https://agent.example/v1/tools/market_regime')
    expect(init.method).toBe('POST')
    expect(init.redirect).toBe('manual')
    expect(init.headers).toEqual({
      'content-type': 'application/json',
      authorization: 'Bearer service-secret',
      'x-wyckoff-user': 'user-1',
    })
    expect(JSON.parse(String(init.body))).toEqual({ args: {}, credentials: {} })
  })

  it('sends only the credentials a tool declares', async () => {
    const fetchImpl = vi.fn(async () => Response.json({ result: {} }))
    const tools = await buildAgentServiceTools(ENV, 'user-1', deps({ fetchImpl: fetchImpl as unknown as typeof fetch }))

    await run(tools, 'intraday_rescue_check', { code: '600519' })
    await run(tools, 'wyckoff_diagnose', { code: '600519' })

    const bodies = fetchImpl.mock.calls.map((call) => JSON.parse(String((call as unknown as [string, RequestInit])[1].body)))
    expect(bodies[0]).toEqual({ args: { code: '600519' }, credentials: { tickflow_api_key: 'tf-key' } })
    expect(bodies[1]).toEqual({ args: { code: '600519' }, credentials: {} })
  })

  it('does not load the user settings for a tool that needs no credentials', async () => {
    const loadCredentials = vi.fn(async () => ({ tickflow_api_key: 'tf-key' }))
    const tools = await buildAgentServiceTools(ENV, 'user-1', deps({ loadCredentials }))

    await run(tools, 'market_regime', {})

    expect(loadCredentials).not.toHaveBeenCalled()
  })

  it('omits empty credentials instead of sending blanks', async () => {
    const fetchImpl = vi.fn(async () => Response.json({ result: {} }))
    const tools = await buildAgentServiceTools(
      ENV,
      'user-1',
      deps({ loadCredentials: async () => ({ tickflow_api_key: '' }), fetchImpl: fetchImpl as unknown as typeof fetch }),
    )

    await run(tools, 'intraday_rescue_check', { code: '600519' })

    expect(JSON.parse(String((fetchImpl.mock.calls[0] as unknown as [string, RequestInit])[1].body)).credentials).toEqual({})
  })

  it.each([
    ['a busy service', 429, '繁忙'],
    ['a rejecting service', 500, '暂时不可用'],
    ['an unknown tool', 404, '暂时不可用'],
  ])('turns %s into an error result the model can explain', async (_name, status, hint) => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const fetchImpl = vi.fn(async () => new Response('internal detail service-secret', { status }))
    const tools = await buildAgentServiceTools(ENV, 'user-1', deps({ fetchImpl: fetchImpl as unknown as typeof fetch }))

    const result = (await run(tools, 'market_regime', {})) as { error: string }

    expect(result.error).toContain(hint)
    expect(result.error).not.toContain('internal detail')
    const line = String(warn.mock.calls[0]?.[0])
    expect(JSON.parse(line)).toMatchObject({ event: 'agent_tools.rejected', tool: 'market_regime', status })
    expect(line).not.toContain('service-secret')
    expect(line).not.toContain('user-1')
  })

  it('turns a network failure into an error result without leaking details', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const fetchImpl = vi.fn(async () => {
      throw new TypeError('connect ECONNREFUSED service-secret')
    })
    const tools = await buildAgentServiceTools(ENV, 'user-1', deps({ fetchImpl: fetchImpl as unknown as typeof fetch }))

    const result = (await run(tools, 'market_regime', {})) as { error: string }

    expect(result.error).toContain('暂时不可用')
    expect(JSON.parse(String(warn.mock.calls[0]?.[0]))).toMatchObject({ event: 'agent_tools.unreachable', error: 'TypeError' })
    expect(String(warn.mock.calls[0]?.[0])).not.toContain('service-secret')
  })

  it('rejects arguments the schema does not accept before calling the service', () => {
    const wyckoff = REMOTE_TOOLS.find((spec) => spec.name === 'wyckoff_diagnose')!
    expect(wyckoff.inputSchema.safeParse({ code: '' }).success).toBe(false)
    expect(wyckoff.inputSchema.safeParse({ code: 'x'.repeat(25) }).success).toBe(false)
    expect(wyckoff.inputSchema.safeParse({}).success).toBe(false)
  })
})
