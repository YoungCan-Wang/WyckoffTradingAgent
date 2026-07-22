import type { AnalyzeStockResult, ScreenResult, StrategyDecisionResult } from '@wyckoff/shared'
import type { TranslationKey } from '@/lib/preferences'
import { asRecord, sanitizeText } from './utils'
import { isToolPart, type MessagePart, type ToolPart } from './messages'

const TOOL_LABEL_KEYS: Record<string, TranslationKey> = {
  search_stock: 'tool.search_stock',
  view_portfolio: 'tool.view_portfolio',
  market_overview: 'tool.market_overview',
  market_history: 'tool.market_history',
  query_recommendations: 'tool.query_recommendations',
  query_attribution: 'tool.query_attribution',
  plan_portfolio_update: 'tool.plan_portfolio_update',
  execute_portfolio_update: 'tool.execute_portfolio_update',
  analyze_stock: 'tool.analyze_stock',
  screen_stocks: 'tool.screen_stocks',
  generate_ai_report: 'tool.generate_ai_report',
  generate_strategy_decision: 'tool.generate_strategy_decision',
  intraday_analysis: 'tool.intraday_analysis',
  run_python_research: 'tool.run_python_research',
}

const TOOL_TONES: Record<string, string> = {
  market_overview: 'border-sky-200 bg-sky-50 text-sky-800 dark:border-sky-500/30 dark:bg-sky-500/10 dark:text-sky-100',
  market_history: 'border-sky-200 bg-sky-50 text-sky-800 dark:border-sky-500/30 dark:bg-sky-500/10 dark:text-sky-100',
  analyze_stock: 'border-violet-200 bg-violet-50 text-violet-800 dark:border-violet-500/30 dark:bg-violet-500/10 dark:text-violet-100',
  screen_stocks: 'border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-100',
  generate_strategy_decision: 'border-rose-200 bg-rose-50 text-rose-800 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-100',
  plan_portfolio_update: 'border-rose-200 bg-rose-50 text-rose-800 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-100',
  execute_portfolio_update: 'border-red-200 bg-red-50 text-red-800 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-100',
  run_python_research: 'border-indigo-200 bg-indigo-50 text-indigo-800 dark:border-indigo-500/30 dark:bg-indigo-500/10 dark:text-indigo-100',
}

export const STRUCTURED_TOOL_NAMES = new Set(['screen_stocks', 'analyze_stock', 'generate_strategy_decision'])
const ACTION_TOOL_NAMES = new Set(['plan_portfolio_update', 'execute_portfolio_update', 'run_python_research'])
const MARKET_INDEX_LABELS: Record<string, string> = {
  sse: '上证',
  csi300: '沪深300',
  szse: '深成指',
  chinext: '创业板',
}

export type AssistantRenderItem =
  | { type: 'text'; content: string; key: string }
  | { type: 'tool'; part: ToolPart; key: string }
  | { type: 'tool-group'; parts: ToolPart[]; key: string }

export function buildAssistantRenderItems(parts: MessagePart[]): AssistantRenderItem[] {
  const items: AssistantRenderItem[] = []
  let pending: ToolPart[] = []
  const flush = () => {
    if (!pending.length) return
    items.push({ type: 'tool-group', parts: pending, key: `tools-${items.length}` })
    pending = []
  }
  parts.forEach((part, index) => appendAssistantPart(items, pending, flush, part, index))
  flush()
  return items
}

function appendAssistantPart(items: AssistantRenderItem[], pending: ToolPart[], flush: () => void, item: MessagePart, index: number) {
  if (item.type === 'text') {
    flush()
    items.push({ type: 'text', content: String(item.text || ''), key: `text-${index}` })
  } else if (isToolPart(item) && shouldRenderStandaloneTool(item)) {
    flush()
    items.push({ type: 'tool', part: item, key: `${item.toolCallId}-${index}` })
  } else if (isToolPart(item)) {
    pending.push(item)
  }
}

export function getToolName(part: ToolPart): string {
  if (part.type === 'dynamic-tool') return String(part.toolName || '')
  return part.type.slice(5)
}

export function formatToolName(toolName: string, t: (key: TranslationKey) => string): string {
  const labelKey = TOOL_LABEL_KEYS[toolName]
  return labelKey ? t(labelKey) : toolName
}

export function isRunningTool(part: ToolPart): boolean {
  return !['output-available', 'output-error', 'output-denied', 'approval-responded'].includes(part.state)
}

export function toolToneClass(toolName: string): string {
  return TOOL_TONES[toolName] || 'border-border bg-background text-foreground'
}

export function toolStateLabel(part: ToolPart, t: (key: TranslationKey) => string): string {
  if (part.state === 'approval-requested') return t('chat.awaitingApproval')
  if (part.state === 'output-denied') return t('chat.denied')
  if (part.state === 'output-available') return t('chat.toolDone')
  if (part.state === 'output-error') return t('chat.requestFailed')
  return t('chat.toolRunning')
}

export function shouldRenderStandaloneTool(part: ToolPart): boolean {
  const toolName = getToolName(part)
  if (ACTION_TOOL_NAMES.has(toolName)) return true
  if (part.state === 'approval-requested' || part.state === 'output-denied') return true
  return STRUCTURED_TOOL_NAMES.has(toolName) && part.state === 'output-available'
}

export function toolGroupTitle(parts: ToolPart[], t: (key: TranslationKey) => string): string {
  const names = new Set(parts.map(getToolName))
  if (names.has('market_overview') || names.has('market_history')) return t('chat.toolGroupMarketData')
  return t('chat.toolGroupDataLookup')
}

export function toolGroupState(parts: ToolPart[], t: (key: TranslationKey) => string, isActive: boolean): string {
  if (parts.some(isRunningTool)) return isActive ? t('chat.toolRunning') : t('chat.toolInterrupted')
  if (parts.some((part) => part.state === 'output-error')) return t('chat.toolGroupPartial')
  return t('chat.toolDone')
}

export function toolGroupStateTone(parts: ToolPart[], isActive: boolean): string {
  if (parts.some(isRunningTool)) return isActive ? 'bg-sky-50 text-sky-700 dark:bg-sky-500/10 dark:text-sky-200' : 'bg-amber-50 text-amber-700 dark:bg-amber-500/10 dark:text-amber-200'
  if (parts.some((part) => part.state === 'output-error')) return 'bg-amber-50 text-amber-700 dark:bg-amber-500/10 dark:text-amber-200'
  return 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-200'
}

export function toolChipLabel(part: ToolPart, t: (key: TranslationKey) => string): string {
  const toolName = getToolName(part)
  const inputLabel = toolInputLabel(toolName, part.input)
  const base = formatToolName(toolName, t)
  return inputLabel ? `${base} · ${inputLabel}` : base
}

export function toolStepMarkerClass(part: ToolPart): string {
  if (part.state === 'output-error') return 'border-red-200 text-red-700 dark:border-red-500/30 dark:text-red-200'
  if (part.state === 'output-denied') return 'border-amber-200 text-amber-700 dark:border-amber-500/30 dark:text-amber-200'
  if (part.state === 'output-available') return 'border-emerald-200 text-emerald-700 dark:border-emerald-500/30 dark:text-emerald-200'
  return 'border-sky-200 text-sky-700 dark:border-sky-500/30 dark:text-sky-200'
}

export function toolStepStateTone(part: ToolPart): string {
  if (part.state === 'output-error') return 'bg-red-50 text-red-700 dark:bg-red-500/10 dark:text-red-200'
  if (part.state === 'output-denied') return 'bg-amber-50 text-amber-700 dark:bg-amber-500/10 dark:text-amber-200'
  if (part.state === 'output-available') return 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-200'
  return 'bg-sky-50 text-sky-700 dark:bg-sky-500/10 dark:text-sky-200'
}

export function toolProgressDescription(toolName: string, input: unknown): string {
  const item = asRecord(input)
  switch (toolName) {
    case 'search_stock':
      return `搜索 ${sanitizeText(item?.query) || '股票代码/名称'}，确认标的基础信息。`
    case 'view_portfolio':
      return '读取当前持仓、成本、止损线和可用资金。'
    case 'market_overview':
      return '读取市场水温、主要指数和风险偏好状态。'
    case 'market_history':
      return `回看 ${marketIndexInputLabel(item)} 的量价结构和威科夫阶段。`
    case 'query_recommendations':
      return `读取最近 ${limitInputLabel(item)} 条形态复盘记录。`
    case 'query_attribution':
      return `读取最近 ${limitInputLabel(item)} 条策略归因报告，检查运营摘要、执行态和 shadow 新增/移除样本。`
    case 'analyze_stock':
      return `诊断 ${sanitizeText(item?.code) || '个股'} 的形态阶段、支撑压力和交易动作。`
    case 'screen_stocks':
      return '读取最新候选池结果，按分数和形态证据筛候选。'
    case 'generate_ai_report':
      return `生成 ${codesInputLabel(item)} 的威科夫深度研报。`
    case 'generate_strategy_decision':
      return '结合市场状态和持仓，生成组合级操作建议。'
    case 'intraday_analysis':
      return `分析 ${sanitizeText(item?.code) || '个股'} 的盘中多周期状态。`
    case 'plan_portfolio_update':
      return '生成调仓方案草稿，等待你确认是否执行。'
    case 'execute_portfolio_update':
      return '执行已确认的持仓变更。'
    case 'run_python_research':
      return `在隔离 Python 沙箱中执行：${sanitizeText(item?.purpose) || '已确认的研究计算'}。`
    default:
      return '读取读盘室相关数据，并把结果交给模型综合判断。'
  }
}

export function toolResultDigest(toolName: string, output: unknown): string {
  if (toolName === 'view_portfolio') return `持仓读取完成：${portfolioResultDigest(output)}`
  if (toolName === 'market_overview') return `市场水温读取完成：${summarizeToolOutput(output)}`
  if (toolName === 'query_recommendations') return `形态复盘记录读取完成：${recordCountDigest(output)}`
  if (toolName === 'query_attribution') return `策略归因读取完成：${recordCountDigest(output)}`
  if (toolName === 'market_history') return `指数历史读取完成：${recordCountDigest(output)}`
  if (toolName === 'run_python_research') return `沙箱计算完成：${sandboxResultDigest(output)}`
  return summarizeToolOutput(output)
}

function sandboxResultDigest(output: unknown): string {
  const item = asRecord(output)
  if (!item) return summarizeToolOutput(output)
  const exitCode = typeof item.exitCode === 'number' ? item.exitCode : null
  const stdout = sanitizeText(item.stdout).replace(/\s+/g, ' ').slice(0, 100)
  return exitCode == null ? summarizeToolOutput(output) : `退出码 ${exitCode}${stdout ? ` · ${stdout}` : ''}`
}

function toolInputLabel(toolName: string, input: unknown): string {
  if (toolName !== 'market_history') return ''
  const value = asRecord(input)
  const index = String(value?.index || 'sse')
  const days = typeof value?.days === 'number' ? `${value.days}日` : ''
  const label = MARKET_INDEX_LABELS[index] || index
  return days ? `${label}/${days}` : label
}

function marketIndexInputLabel(item: Record<string, unknown> | null): string {
  const index = String(item?.index || 'sse')
  const days = typeof item?.days === 'number' ? item.days : 100
  return `${MARKET_INDEX_LABELS[index] || index} 近 ${days} 个交易日`
}

function limitInputLabel(item: Record<string, unknown> | null): string {
  return typeof item?.limit === 'number' ? String(item.limit) : '若干'
}

function codesInputLabel(item: Record<string, unknown> | null): string {
  const codes = Array.isArray(item?.codes) ? item.codes.map((code) => sanitizeText(code)).filter(Boolean) : []
  if (codes.length === 0) return '指定标的'
  return codes.slice(0, 4).join('、')
}

function portfolioResultDigest(output: unknown): string {
  const item = asRecord(output)
  const positions = Array.isArray(item?.positions) ? item.positions.length : null
  const cash = typeof item?.cash === 'number' ? `，现金 ${item.cash.toFixed(0)}` : ''
  return positions == null ? summarizeToolOutput(output) : `${positions} 只持仓${cash}`
}

function recordCountDigest(output: unknown): string {
  if (Array.isArray(output)) return `${output.length} 条记录`
  const item = asRecord(output)
  if (!item) return summarizeToolOutput(output)
  for (const key of ['records', 'items', 'rows', 'data', 'history', 'recommendations']) {
    const value = item[key]
    if (Array.isArray(value)) return `${value.length} 条记录`
  }
  return summarizeToolOutput(output)
}

export function isScreenResult(value: unknown): value is ScreenResult {
  const item = asRecord(value)
  return Boolean(item && typeof item.date === 'string' && Array.isArray(item.stocks) && asRecord(item.meta))
}

export function isAnalyzeResult(value: unknown): value is AnalyzeStockResult {
  const item = asRecord(value)
  return Boolean(item && typeof item.summary === 'string' && typeof item.phase === 'string' && typeof item.markdown === 'string')
}

export function isStrategyResult(value: unknown): value is StrategyDecisionResult {
  const item = asRecord(value)
  return Boolean(item && typeof item.summary === 'string' && Array.isArray(item.position_actions))
}

export function summarizeToolOutput(value: unknown): string {
  if (typeof value === 'string') return value.replace(/\s+/g, ' ').slice(0, 160)
  if (Array.isArray(value)) return `${value.length} rows`
  const item = asRecord(value)
  if (!item) return String(value ?? '-')
  return Object.keys(item).slice(0, 4).map((key) => `${key}: ${formatPreviewValue(item[key])}`).join(' · ')
}

function formatPreviewValue(value: unknown): string {
  if (Array.isArray(value)) return `${value.length} rows`
  if (value && typeof value === 'object') return 'object'
  return String(value ?? '-').slice(0, 40)
}
