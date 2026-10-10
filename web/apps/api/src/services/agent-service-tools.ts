import { tool, type ToolSet } from 'ai'
import { z } from 'zod'
import type { Env } from '../app'
import { agentServiceConfig, isListedUser, type AgentServiceConfig } from './agent-lane'

// 读盘室的循环留在 Worker 里，会员额外获得由 Python 服务提供的工具。
// 这里只放读盘室里**没有**的能力：同名或功能重复的工具不要加，会员已经有它们和专用的展示。

type CredentialKey = 'tushare_token' | 'tickflow_api_key'

type RemoteToolSpec = {
  name: string
  description: string
  inputSchema: z.ZodObject<z.ZodRawShape>
  credentials: CredentialKey[]
}

const CODE_SCHEMA = z.string().trim().min(1).max(24)

export const REMOTE_TOOLS: RemoteToolSpec[] = [
  {
    name: 'market_regime',
    description:
      'A 股市况判定与动态阈值：基于沪深 300 与创业板的趋势、均线、量能给出 regime（如 RISK_OFF）、结构性判定和次日量价推演。' +
      'market_overview 只给原始行情，不含这个判定；回答仓位倾向、能不能进攻时应先调用它。',
    inputSchema: z.object({}),
    credentials: [],
  },
  {
    name: 'wyckoff_diagnose',
    description:
      '单股威科夫结构诊断（确定性引擎，不经模型）：交易区间、触发信号（spring / sos / lps / evr）、所处阶段与事件分类。' +
      '阶段只用于诊断解读，不等于可执行决策。需要具体价位和操作建议时再配合 analyze_stock。',
    inputSchema: z.object({ code: CODE_SCHEMA }),
    credentials: [],
  },
  {
    name: 'intraday_rescue_check',
    description:
      '60 分钟结构救援评估：平台突破、VWAP 收复、趋势确立，用于判断被套持仓盘中是否出现自救结构。' +
      '需要用户在设置页配置 TickFlow Key，非交易时段可能取不到数据。',
    inputSchema: z.object({ code: CODE_SCHEMA }),
    credentials: ['tickflow_api_key'],
  },
]

const CALL_TIMEOUT_MS = 90_000

export type AgentToolsDeps = {
  isMember: () => Promise<boolean>
  loadCredentials: () => Promise<Partial<Record<CredentialKey, string>>>
  fetchImpl?: typeof fetch
}

// 只记事件、请求 ID 和状态码：不记用户 ID、参数、凭据或服务返回内容。
function logTool(event: string, detail: Record<string, unknown>): void {
  console.warn(JSON.stringify({ event, timestamp: new Date().toISOString(), ...detail }))
}

async function callRemoteTool(
  config: AgentServiceConfig,
  userId: string,
  spec: RemoteToolSpec,
  args: Record<string, unknown>,
  deps: AgentToolsDeps,
): Promise<unknown> {
  const available = spec.credentials.length > 0 ? await deps.loadCredentials() : {}
  const credentials = Object.fromEntries(
    spec.credentials.flatMap((key) => (available[key] ? [[key, available[key]]] : [])),
  )
  let response: Response
  try {
    response = await (deps.fetchImpl ?? fetch)(`${config.baseUrl}/v1/tools/${spec.name}`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        authorization: `Bearer ${config.token}`,
        'x-wyckoff-user': userId,
      },
      body: JSON.stringify({ args, credentials }),
      signal: AbortSignal.timeout(CALL_TIMEOUT_MS),
      redirect: 'manual',
    })
  } catch (error) {
    logTool('agent_tools.unreachable', { tool: spec.name, error: error instanceof Error ? error.name : 'unknown' })
    return { error: '工具服务暂时不可用，请稍后重试。' }
  }
  if (!response.ok) {
    logTool('agent_tools.rejected', { tool: spec.name, status: response.status })
    await response.body?.cancel()
    return { error: response.status === 429 ? '工具服务繁忙，请稍后重试。' : '工具服务暂时不可用，请稍后重试。' }
  }
  const body = (await response.json().catch(() => null)) as { result?: unknown } | null
  return body && 'result' in body ? body.result : { error: '工具服务返回了无法识别的结果。' }
}

// 三道闸门同时满足才给模型这些工具：服务已配置、用户在 AGENT_TOOLS_USERS 里、且是有效星球会员。
export async function buildAgentServiceTools(env: Env, userId: string, deps: AgentToolsDeps): Promise<ToolSet> {
  const config = agentServiceConfig(env)
  if (!config || !isListedUser(env.AGENT_TOOLS_USERS, userId)) return {}
  if (!(await deps.isMember())) return {}
  return Object.fromEntries(
    REMOTE_TOOLS.map((spec) => [
      spec.name,
      tool({
        description: spec.description,
        inputSchema: spec.inputSchema,
        execute: (args) => callRemoteTool(config, userId, spec, args as Record<string, unknown>, deps),
      }),
    ]),
  )
}
