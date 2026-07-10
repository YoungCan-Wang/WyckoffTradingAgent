import { BellPlus, Plus, ShieldAlert } from 'lucide-react'
import { formatStrategyPolicyText, type AnalyzeStockResult, type ScreenStockItem, type StrategyDecisionResult } from '@wyckoff/shared'
import { MarkdownContent } from '@/components/markdown'
import { ScreenResultCard } from '@/components/screen-result-card'
import { asRecord, normalizeStockCode, sanitizeText } from './utils'
import type { PinStockInput } from './types'
import { isAnalyzeResult, isScreenResult, isStrategyResult, summarizeToolOutput } from './tool-rendering-model'

export function ToolStructuredOutput({
  toolName,
  input,
  output,
  onPinStock,
}: {
  toolName: string
  input: unknown
  output: unknown
  onPinStock: (item: PinStockInput) => void
}) {
  if (toolName === 'screen_stocks' && isScreenResult(output)) {
    return <ScreenResultCard data={output} onPinStock={(stock) => onPinStock(pinFromScreenStock(stock))} />
  }
  if (toolName === 'analyze_stock' && isAnalyzeResult(output)) return <AnalyzeResultCard data={output} input={input} onPinStock={onPinStock} />
  if (toolName === 'generate_strategy_decision' && isStrategyResult(output)) return <StrategyResultCard data={output} onPinStock={onPinStock} />
  if (output == null) return null
  return <p className="mt-1 line-clamp-2 text-[11px] opacity-80">{summarizeToolOutput(output)}</p>
}

function pinFromScreenStock(stock: ScreenStockItem): PinStockInput {
  return {
    code: stock.code,
    name: stock.name,
    reason: stock.funnel_score != null ? `漏斗分 ${stock.funnel_score.toFixed(2)} 的候选股` : '漏斗选股候选',
    source: '漏斗选股',
    trigger: '等待放量突破、缩量回踩或尾盘确认',
    invalidation: '跌破形态关键支撑或后续证据转弱',
    score: stock.funnel_score,
    changePct: stock.change_pct,
  }
}

function AnalyzeResultCard({
  data,
  input,
  onPinStock,
}: {
  data: AnalyzeStockResult
  input: unknown
  onPinStock: (item: PinStockInput) => void
}) {
  const inputRecord = asRecord(input)
  const code = normalizeStockCode(inputRecord?.code || inputRecord?.symbol || inputRecord?.ts_code)
  const name = sanitizeText(inputRecord?.name)

  return (
    <div className="mt-2 space-y-2 rounded-lg border border-border/50 bg-background/50 p-3">
      <AnalyzeResultHeader data={data} code={code} name={name} onPinStock={onPinStock} />
      <div className="grid gap-2 text-[11px] sm:grid-cols-3">
        <DecisionMetric label="阶段" value={data.phase} />
        <DecisionMetric label="动作" value={data.action} />
        <DecisionMetric label="置信" value={data.confidence != null ? data.confidence.toFixed(0) : '--'} />
      </div>
      <AnalyzeLevelBadges data={data} />
      <MarkdownContent content={data.markdown || data.summary} className="text-xs" />
    </div>
  )
}

function AnalyzeResultHeader({
  data,
  code,
  name,
  onPinStock,
}: {
  data: AnalyzeStockResult
  code: string
  name: string
  onPinStock: (item: PinStockInput) => void
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-2">
      <div>
        <div className="text-[11px] text-muted-foreground">个股决策卡</div>
        <p className="text-sm font-semibold">{code ? `${code}${name ? ` ${name}` : ''}` : data.summary}</p>
      </div>
      {code && <PinAnalyzeButton data={data} code={code} name={name} onPinStock={onPinStock} />}
    </div>
  )
}

function PinAnalyzeButton({
  data,
  code,
  name,
  onPinStock,
}: {
  data: AnalyzeStockResult
  code: string
  name: string
  onPinStock: (item: PinStockInput) => void
}) {
  return (
    <button type="button" onClick={() => onPinStock(pinFromAnalyze(data, code, name))} className="inline-flex shrink-0 items-center gap-1 rounded-md border border-border bg-background px-2 py-1 text-[11px] text-muted-foreground hover:bg-muted/70 hover:text-foreground">
      <BellPlus size={12} />
      观察
    </button>
  )
}

function pinFromAnalyze(data: AnalyzeStockResult, code: string, name: string): PinStockInput {
  return {
    code,
    name,
    reason: data.action || data.summary,
    source: '个股诊断',
    trigger: data.resistance ? `突破或站稳 ${data.resistance}` : '等待关键位确认',
    invalidation: data.support ? `跌破 ${data.support}` : data.risk,
    phase: data.phase,
    action: data.action,
  }
}

function AnalyzeLevelBadges({ data }: { data: AnalyzeStockResult }) {
  return (
    <div className="flex flex-wrap gap-2 text-[11px]">
      {data.support && <span className="rounded-full bg-down/10 px-2 py-0.5 text-down">支撑 {data.support}</span>}
      {data.resistance && <span className="rounded-full bg-up/10 px-2 py-0.5 text-up">压力 {data.resistance}</span>}
      {data.risk && <span className="rounded-full bg-amber-50 px-2 py-0.5 text-amber-700 dark:bg-amber-500/10 dark:text-amber-200">风险 {data.risk}</span>}
    </div>
  )
}

function DecisionMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-border/50 bg-background/60 px-2 py-1.5">
      <div className="text-muted-foreground">{label}</div>
      <div className="mt-0.5 truncate font-medium text-foreground">{value || '--'}</div>
    </div>
  )
}

function StrategyResultCard({
  data,
  onPinStock,
}: {
  data: StrategyDecisionResult
  onPinStock: (item: PinStockInput) => void
}) {
  return (
    <div className="mt-2 space-y-2 rounded-lg border border-border/50 bg-background/50 p-3 text-xs">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="text-[11px] text-muted-foreground">组合策略卡</div>
          <p className="font-semibold">{data.summary}</p>
        </div>
        <ShieldAlert size={16} className="shrink-0 text-amber-600" />
      </div>
      <div className="grid gap-2 text-[11px] sm:grid-cols-2">
        <DecisionMetric label="市场环境" value={data.market_regime} />
        <DecisionMetric label="总仓位" value={data.overall_position} />
      </div>
      <StrategyPolicyLine policy={data.strategy_policy} />
      <StrategyActionList actions={data.position_actions} onPinStock={onPinStock} />
      <p className="text-muted-foreground">组合风险：{data.risk}</p>
    </div>
  )
}

function StrategyPolicyLine({ policy }: { policy?: StrategyDecisionResult['strategy_policy'] }) {
  const text = formatStrategyPolicyText(policy)
  if (!text) return null
  return (
    <div className="rounded-md border border-amber-200/70 bg-amber-50/70 px-2 py-1.5 text-[11px] leading-relaxed text-amber-800 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-100">
      <span className="font-medium">策略治理：</span>
      <span className="break-words">{text}</span>
    </div>
  )
}

function StrategyActionList({
  actions,
  onPinStock,
}: {
  actions: StrategyDecisionResult['position_actions']
  onPinStock: (item: PinStockInput) => void
}) {
  if (actions.length === 0) return null
  return (
    <div className="space-y-1.5">
      {actions.map((item) => <StrategyActionCard key={`${item.code}-${item.action}`} item={item} onPinStock={onPinStock} />)}
    </div>
  )
}

function StrategyActionCard({
  item,
  onPinStock,
}: {
  item: StrategyDecisionResult['position_actions'][number]
  onPinStock: (item: PinStockInput) => void
}) {
  return (
    <div className="rounded-md border border-border/50 bg-background/60 px-2 py-1.5">
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0 font-medium">
          <span className="font-mono">{item.code}</span> {item.name || ''} · {item.action}
        </div>
        <button type="button" onClick={() => onPinStock(pinFromStrategy(item))} className="inline-flex shrink-0 items-center gap-1 rounded-md px-2 py-1 text-[11px] text-muted-foreground hover:bg-muted/70 hover:text-foreground">
          <Plus size={12} />
          观察
        </button>
      </div>
      <div className="mt-0.5 text-muted-foreground">{item.reason}</div>
      {item.risk && <div className="mt-0.5 text-amber-700 dark:text-amber-200">风险：{item.risk}</div>}
    </div>
  )
}

function pinFromStrategy(item: StrategyDecisionResult['position_actions'][number]): PinStockInput {
  return {
    code: item.code,
    name: item.name,
    reason: item.reason,
    source: '策略建议',
    trigger: item.action,
    invalidation: item.risk,
    action: item.action,
  }
}
