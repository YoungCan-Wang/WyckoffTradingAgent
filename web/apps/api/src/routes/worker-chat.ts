import { tool, type ToolSet } from 'ai'
import { z } from 'zod'
import type { Env } from '../app'
import { isActiveWhitelistUser } from '../middleware/whitelist'
import { PYTHON_RESEARCH_SCRIPT_SCHEMA, enqueuePythonResearch } from '../services/agent-run'
import { createChatRoutes, createUserSupabase } from './chat'

export const workerChatRoutes = createChatRoutes(buildSandboxTools)

function buildSandboxTools(env: Env, userId: string, accessToken: string, requestId: string): ToolSet {
  if (env.AGENT_SANDBOX_ENABLED !== 'true') return {}
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
  if (!(await isActiveWhitelistUser(supabase, userId))) throw new Error('Agent sandbox requires whitelist access')
  return enqueuePythonResearch(env, userId, script, { requestId })
}
