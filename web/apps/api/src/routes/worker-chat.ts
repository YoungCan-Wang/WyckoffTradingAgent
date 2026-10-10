import { tool, type ToolSet } from 'ai'
import { z } from 'zod'
import type { Env } from '../app'
import { isActivePlanetMember } from '../middleware/planet-membership'
import { PYTHON_RESEARCH_SCRIPT_SCHEMA, enqueuePythonResearch } from '../services/agent-run'
import { buildAgentServiceTools } from '../services/agent-service-tools'
import { createChatRoutes, createUserSupabase } from './chat'

export const workerChatRoutes = createChatRoutes(buildWorkerTools)

// 读盘室循环之外，会员额外获得的工具：隔离沙箱研究计算，以及由 Python 服务提供的引擎类工具。
export async function buildWorkerTools(env: Env, userId: string, accessToken: string, requestId: string): Promise<ToolSet> {
  const [sandbox, agentService] = await Promise.all([
    buildSandboxTools(env, userId, accessToken, requestId),
    buildAgentServiceTools(env, userId, agentServiceDeps(env, userId, accessToken)),
  ])
  return { ...sandbox, ...agentService }
}

function agentServiceDeps(env: Env, userId: string, accessToken: string) {
  // 客户端在用到时才创建：没配置服务或用户不在名单里时，请求路径上什么都不多做。
  const supabase = () => createUserSupabase(env, accessToken)
  return {
    isMember: () => isActivePlanetMember(supabase(), userId),
    loadCredentials: async () => {
      const { data } = await supabase()
        .from('user_settings')
        .select('tickflow_api_key, tushare_token')
        .eq('user_id', userId)
        .single()
      const row = (data || {}) as Record<string, unknown>
      return {
        tickflow_api_key: String(row.tickflow_api_key || '').trim(),
        tushare_token: String(row.tushare_token || '').trim(),
      }
    },
  }
}

export async function buildSandboxTools(env: Env, userId: string, accessToken: string, requestId: string): Promise<ToolSet> {
  if (env.AGENT_SANDBOX_ENABLED !== 'true') return {}
  // Hide the tool from non-members so the model never proposes a run
  // that would only fail after the user has already approved it.
  if (!(await isActivePlanetMember(createUserSupabase(env, accessToken), userId))) return {}
  return {
    run_python_research: tool({
      description: '将有限的 Python 研究计算排入无网络、无密钥、执行后删除的沙箱。只能在用户明确要求后使用；脚本仅可处理本轮已知的有限数据。工具会返回 runId，随后可查询短期保存的结果。',
      inputSchema: z.object({
        purpose: z.string().trim().min(1).max(240),
        script: PYTHON_RESEARCH_SCRIPT_SCHEMA,
      }),
      needsApproval: true,
      execute: async ({ script }) => runApprovedPythonResearch(env, userId, accessToken, requestId, script),
    }),
  }
}

async function runApprovedPythonResearch(env: Env, userId: string, accessToken: string, requestId: string, script: string) {
  const supabase = createUserSupabase(env, accessToken)
  if (!(await isActivePlanetMember(supabase, userId))) throw new Error('Agent sandbox requires planet membership')
  return enqueuePythonResearch(env, userId, script, { requestId })
}
