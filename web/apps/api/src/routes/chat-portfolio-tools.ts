import {
  execExecutePortfolioUpdate,
  type ToolDeps,
} from '@wyckoff/shared'
import { tool, type ToolSet } from 'ai'
import { z } from 'zod'
import { isActivePlanetMember } from '../middleware/planet-membership'

const PORTFOLIO_UPDATE_SCHEMA = z.object({
  action: z.enum(['add', 'update', 'delete']),
  code: z.string().describe('A股6位 / 港股00700.HK / 美股AAPL.US'),
  name: z.string().nullable(),
  shares: z.number().nullable(),
  cost_price: z.number().nullable(),
  stop_loss: z.number().nullable(),
  buy_dt: z.string().nullable().describe('建仓日 YYYYMMDD 或 YYYY-MM-DD；新增必填且须为真实日期，改股数/成本时不要传。update 目标不存在时报错，不会新建'),
})

export function buildCloudPortfolioTools(
  deps: ToolDeps,
  userId: string,
  options: { enabled: boolean },
): ToolSet {
  if (!options.enabled) return {}
  return {
    plan_portfolio_update: tool({
      description: '生成调仓方案（不执行）。',
      inputSchema: PORTFOLIO_UPDATE_SCHEMA.extend({ reason: z.string().nullable() }),
      execute: formatPortfolioPlan,
    }),
    execute_portfolio_update: tool({
      description: '执行调仓。此工具必须经过用户审批。新增必须带合法建仓日 buy_dt（YYYYMMDD 或 YYYY-MM-DD），改股数/成本不要传 buy_dt；update 目标不存在时报错，不会新建。',
      inputSchema: PORTFOLIO_UPDATE_SCHEMA,
      needsApproval: true,
      execute: ({ action, code, name, shares, cost_price, stop_loss, buy_dt }) =>
        executeApprovedCloudPortfolioUpdate(deps, userId, action, code, name, shares, cost_price, stop_loss, buy_dt),
    }),
  }
}

export async function executeApprovedCloudPortfolioUpdate(
  deps: ToolDeps,
  userId: string,
  action: 'add' | 'update' | 'delete',
  code: string,
  name: string | null,
  shares: number | null,
  cost_price: number | null,
  stop_loss: number | null,
  buy_dt: string | null,
): Promise<string> {
  // Defense in depth: registration may have been enabled at request start.
  if (!(await isActivePlanetMember(deps.supabase, userId))) {
    return '执行失败：云端持仓同步需要有效星球会员'
  }
  return execExecutePortfolioUpdate(deps, userId, action, code, name, shares, cost_price, stop_loss, buy_dt)
}

function formatPortfolioPlan(params: z.infer<typeof PORTFOLIO_UPDATE_SCHEMA> & { reason: string | null }) {
  const actionLabel = { add: '新增', update: '修改', delete: '删除' }[params.action]
  return [
    `📋 **调仓方案**`,
    `- 操作：${actionLabel}`,
    `- 标的：${params.code} ${params.name || ''}`,
    params.shares ? `- 股数：${params.shares}` : '',
    params.cost_price ? `- 价格：¥${params.cost_price}` : '',
    params.stop_loss ? `- 止损：¥${params.stop_loss}` : '',
    params.reason ? `- 理由：${params.reason}` : '',
    '',
    '⚠️ 请确认是否执行此操作？',
  ].filter(Boolean).join('\n')
}
