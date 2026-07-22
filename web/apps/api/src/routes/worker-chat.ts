import { tool, type ToolSet } from 'ai'
import { z } from 'zod'
import type { Env } from '../app'
import { isActiveWhitelistUser } from '../middleware/whitelist'
import { PYTHON_RESEARCH_SCRIPT_SCHEMA, runPythonResearch } from '../services/agent-run'
import { createChatRoutes, createUserSupabase } from './chat'

export const workerChatRoutes = createChatRoutes(buildSandboxTools)

function buildSandboxTools(env: Env, userId: string, accessToken: string): ToolSet {
  if (env.AGENT_SANDBOX_ENABLED !== 'true') return {}
  return {
    run_python_research: tool({
      description: '在无网络、无密钥、执行后删除的 Python 沙箱中运行一次有限的研究计算。只能在用户明确要求后使用；脚本仅可处理本轮已知的有限数据，结果会短期保存。',
      inputSchema: z.object({
        purpose: z.string().trim().min(1).max(240),
        script: PYTHON_RESEARCH_SCRIPT_SCHEMA,
      }),
      needsApproval: true,
      execute: async ({ script }) => runApprovedPythonResearch(env, userId, accessToken, script),
    }),
  }
}

async function runApprovedPythonResearch(env: Env, userId: string, accessToken: string, script: string) {
  const supabase = createUserSupabase(env, accessToken)
  if (!(await isActiveWhitelistUser(supabase, userId))) throw new Error('Agent sandbox requires whitelist access')
  return runPythonResearch(env, userId, script)
}
