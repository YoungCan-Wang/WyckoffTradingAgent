import { useMemo, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'
import {
  attributionExecutionImpactText,
  attributionFormalDynamicLabel,
  attributionFormalDynamicReasonLabel,
  attributionGovernorStatusLabel,
  attributionModeRecommendationLabel,
  attributionNextActionLabel,
  attributionOperatorSummary as buildAttributionOperatorSummary,
  attributionPromotionStatusLabel,
  policyExecutionModeLabel,
} from '@wyckoff/shared'
import { supabase } from '@/lib/supabase'
import { usePlanetMembership, planetMembershipGateView } from '@/lib/planet-membership-gate'
import { WyckoffLoading } from '@/components/loading'
import { financialValueClass } from '@/lib/financial-colors'
import { useAuthStore } from '@/stores/auth'

type JsonMap = Record<string, unknown>

interface AttributionReport {
  report_date: string
  market: string
  window_start: string
  window_end: string
  horizons: number[]
  signal_stats_json: Record<string, Record<string, MetricStats>>
  score_bucket_stats_json?: JsonMap
  shadow_diff_stats_json: JsonMap
  top_winners_json: StockOutcome[]
  top_losers_json: StockOutcome[]
  recommendations_json: AttributionRecommendation[]
  created_at: string
}

interface MetricStats {
  count?: number
  avg_return_pct?: number
  median_return_pct?: number
  win_rate_pct?: number
  big_win_rate_pct?: number
  big_loss_rate_pct?: number
  avg_drawdown_pct?: number | null
}

interface ObservationCoverageMetric {
  observations?: number
  with_any_outcome?: number
  outcome_coverage_pct?: number
  features_coverage_pct?: number
  current_like_pct?: number
  legacy_like_pct?: number
  latest_trade_date?: string
  h1_coverage_pct?: number
  h3_coverage_pct?: number
  h5_coverage_pct?: number
  h10_coverage_pct?: number
  h20_coverage_pct?: number
}

interface StockOutcome {
  trade_date?: string
  code?: string
  name?: string | null
  signal_type?: string
  track?: string
  return_pct?: number
  candidate_shadow_score?: number | null
  candidate_shadow_grade?: string | null
  entry_quality_score?: number | null
  entry_quality_grade?: string | null
  entry_quality_risk_flags?: string[] | null
  data_lineage_coverage_score?: number | null
  data_lineage_coverage_grade?: string | null
  data_lineage_evidence_keys?: string[] | null
  selection_mode?: string | null
  strategy_version?: string | null
  candidate_lane?: string | null
  entry_type?: string | null
}

interface AttributionRecommendation {
  type?: string
  horizon?: string
  target?: string
  reason?: string
}

interface PolicyGovernor {
  status?: string
  horizon?: string
  auto_apply?: boolean
  mode_recommendation?: string
  next_action?: string
  next_action_summary?: string
  promotion_status?: string
  promotion_checklist?: PromotionCheck[]
  summary?: string
  shadow_gate?: JsonMap
  signal_actions?: PolicySignalAction[]
}

interface PromotionCheck {
  key?: string
  status?: string
  summary?: string
}

interface PolicySignalAction {
  action?: string
  horizon?: string
  target?: string
  weight_multiplier?: number
  evidence?: MetricStats
  scope?: Record<string, string>
}

interface PolicyActionDetail {
  action?: string
  horizon?: string
  target?: string
  label?: string
  weight_multiplier?: number
  scope?: Record<string, string>
  evidence?: MetricStats
}

interface PolicyExecutionStats {
  actionCount: number
  downCount: number
  upCount: number
  otherCount: number
  targets: string[]
}

interface PolicyExecutionPayload {
  funnel_dynamic_policy?: string
  horizon?: string
  next_action?: string
  next_action_summary?: string
  signal_action_count?: number
  action_details?: PolicyActionDetail[]
  formal_dynamic_allowed?: boolean
  formal_dynamic_block_reason?: string
  promotion_status?: string
  promotion_checklist?: PromotionCheck[]
  scope?: string
  active_scope?: string
  funnel_shadow_weights_active?: boolean
  funnel_formal_weights_active?: boolean
  summary?: string
}

interface PolicyOperationsPayload {
  operator_summary?: string
  action_summary?: string
  backtest_confirmation_text?: string
  promotion_checklist_summary?: string
}

async function fetchLatestReport(): Promise<AttributionReport | null> {
  const { data, error } = await supabase
    .from('strategy_attribution_reports')
    .select('*')
    .eq('market', 'cn')
    .order('report_date', { ascending: false })
    .limit(1)
    .maybeSingle()
  if (error) throw new Error(error.message)
  return data
}

export function AttributionPage() {
  const user = useAuthStore((s) => s.user)
  const userId = user?.id
  const membership = usePlanetMembership(userId)
  const report = useQuery({
    queryKey: ['strategy-attribution-report'],
    queryFn: fetchLatestReport,
    enabled: membership.data?.isActive === true,
  })

  const gateView = planetMembershipGateView(membership, <WyckoffLoading />, <LockedView />)
  if (gateView) return gateView
  if (report.isLoading) return <WyckoffLoading />

  return (
    <div className="h-full overflow-auto p-6">
      <div className="mb-6 flex items-start justify-between gap-4">
        <div>
          <p className="text-xs font-medium uppercase tracking-[0.18em] text-muted-foreground">Strategy Attribution</p>
          <h1 className="mt-2 text-2xl font-semibold tracking-tight">策略归因报告</h1>
          <p className="mt-2 max-w-3xl text-sm text-muted-foreground">
            固定周期结果、形态表现、分数分桶和 shadow 差异的聚合视图。信号级调权会作为漏斗动态策略输入；
            是否把动态策略从 shadow 晋级到 on 仍需人工确认。
          </p>
        </div>
        <button
          type="button"
          onClick={() => void report.refetch()}
          className="inline-flex items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm text-muted-foreground hover:bg-muted hover:text-foreground"
        >
          <RefreshCw size={15} />
          刷新
        </button>
      </div>
      {report.error ? <ErrorBox message={report.error.message} /> : report.data ? <ReportView report={report.data} /> : <EmptyView />}
    </div>
  )
}

function ReportView({ report }: { report: AttributionReport }) {
  const signalRows = useMemo(() => flattenSignalStats(report.signal_stats_json), [report.signal_stats_json])
  const candidateShadowRows = useMemo(
    () => flattenCandidateShadowStats(report.score_bucket_stats_json),
    [report.score_bucket_stats_json],
  )
  const entryQualityRows = useMemo(
    () => flattenEntryQualityStats(report.score_bucket_stats_json),
    [report.score_bucket_stats_json],
  )
  const dataLineageRows = useMemo(
    () => flattenDataLineageStats(report.score_bucket_stats_json),
    [report.score_bucket_stats_json],
  )
  const observationCoverageRows = useMemo(
    () => flattenObservationCoverage(report.score_bucket_stats_json),
    [report.score_bucket_stats_json],
  )
  const governor = useMemo(() => policyGovernor(report.shadow_diff_stats_json), [report.shadow_diff_stats_json])
  const executionPayload = useMemo(
    () => policyExecutionPayload(report.shadow_diff_stats_json),
    [report.shadow_diff_stats_json],
  )
  const operationsPayload = useMemo(
    () => policyOperationsPayload(report.shadow_diff_stats_json),
    [report.shadow_diff_stats_json],
  )
  const policyExecution = useMemo(
    () => policyExecutionStats(report.recommendations_json, executionPayload?.horizon),
    [report.recommendations_json, executionPayload?.horizon],
  )
  return (
    <div className="space-y-6">
      <Summary report={report} />
      <OperationsBrief
        shadow={report.shadow_diff_stats_json}
        execution={executionPayload}
        operations={operationsPayload}
      />
      <ObservationCoverage rows={observationCoverageRows} />
      <PolicyGovernorBox governor={governor} />
      <PolicyExecutionState stats={policyExecution} governor={governor} execution={executionPayload} />
      <Recommendations rows={report.recommendations_json} />
      <CandidateShadowStats rows={candidateShadowRows} />
      <EntryQualityStats rows={entryQualityRows} />
      <DataLineageStats rows={dataLineageRows} />
      <SignalStats rows={signalRows} />
      <OutcomeTables winners={report.top_winners_json} losers={report.top_losers_json} />
      <ShadowBox data={report.shadow_diff_stats_json} />
    </div>
  )
}

function Summary({ report }: { report: AttributionReport }) {
  return (
    <section className="grid gap-3 md:grid-cols-5">
      <MetricCard label="报告日期" value={report.report_date} />
      <MetricCard label="样本窗口" value={`${report.window_start} ~ ${report.window_end}`} />
      <MetricCard label="周期" value={report.horizons.join('/')} />
      <MetricCard label="生成时间" value={formatDateTime(report.created_at)} />
      <MetricCard label="数据来源" value="远端 strategy_attribution_reports" />
    </section>
  )
}

function OperationsBrief({
  shadow,
  execution,
  operations,
}: {
  shadow: JsonMap
  execution: PolicyExecutionPayload | null
  operations: PolicyOperationsPayload | null
}) {
  const latest = shadowLatest(shadow)
  const selection = latestSelection(latest)
  const actions = execution?.action_details || []
  const operatorSummary = buildAttributionOperatorSummary({
    operations,
    execution,
    latest,
    selection,
    actions,
  })
  return (
    <Panel title="运营复盘">
      <div className="mb-3 rounded-md border border-border/70 bg-muted/30 px-3 py-2 text-sm text-foreground">
        {operatorSummary}
      </div>
      <div className="grid gap-3 md:grid-cols-6">
        <MetricCard label="最新 Shadow" value={`${String(latest?.trade_date || '-')} · ${String(latest?.regime || '-')}`} />
        <MetricCard label="Base → Shadow" value={`${fmtCount(selection?.base_count)} → ${fmtCount(selection?.shadow_count)}`} />
        <MetricCard label="新增 / 移除" value={`${fmtCount(selection?.diff_added_count)} / ${fmtCount(selection?.diff_removed_count)}`} />
        <MetricCard label="Jaccard" value={fmtScoreNumber(selection?.jaccard)} />
        <MetricCard label="回测确认" value={operations?.backtest_confirmation_text || '-'} />
        <MetricCard label="晋级清单" value={operations?.promotion_checklist_summary || '-'} />
      </div>
      <div className="mt-3 grid gap-2 text-sm md:grid-cols-2">
        <p className="text-muted-foreground">新增样本：{formatCodeSample(latest?.diff_added_sample)}</p>
        <p className="text-muted-foreground">移除样本：{formatCodeSample(latest?.diff_removed_sample)}</p>
      </div>
      <div className="mt-4 text-sm">
        <div className="mb-2 font-medium">本期可执行调权</div>
        {actions.length ? (
          <div className="space-y-2">
            {actions.slice(0, 6).map((item, index) => (
              <div key={`${item.label}-${item.action}-${index}`} className="rounded-md border border-border/70 px-3 py-2">
                <div className="font-medium">
                  {formatPolicyAction(item.action)} · {item.label || formatPolicySignalTarget(item.target, item.scope)} · x{fmtWeight(item.weight_multiplier)}
                </div>
                <div className="mt-1 text-xs text-muted-foreground">
                  均收 {fmtPct(item.evidence?.avg_return_pct)} · 胜率 {fmtPct(item.evidence?.win_rate_pct)} · 回撤 {fmtPct(item.evidence?.avg_drawdown_pct)}
                </div>
              </div>
            ))}
            {actions.length > 6 ? <p className="text-xs text-muted-foreground">另有 {actions.length - 6} 项调权在策略建议中查看。</p> : null}
          </div>
        ) : (
          <p className="text-muted-foreground">暂无可执行调权。</p>
        )}
      </div>
    </Panel>
  )
}

function ObservationCoverage({
  rows,
}: {
  rows: Array<{ group: string; value: string; stats: ObservationCoverageMetric }>
}) {
  if (!rows.length) {
    return (
      <Panel title="数据口径与覆盖">
        <p className="text-sm text-muted-foreground">当前归因快照缺少覆盖率元数据，等待下一次归因任务刷新。</p>
      </Panel>
    )
  }
  return (
    <Panel title="数据口径与覆盖">
      <div className="overflow-auto">
        <table className="w-full min-w-[980px] text-left text-sm">
          <thead className="text-xs text-muted-foreground">
            <tr className="border-b border-border">
              <th className="py-2">口径</th>
              <th>值</th>
              <th>观察</th>
              <th>有结果</th>
              <th>总覆盖</th>
              <th>h1</th>
              <th>h3</th>
              <th>h5</th>
              <th>证据覆盖</th>
              <th>当前口径</th>
              <th>旧口径</th>
              <th>最新日期</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ group, value, stats }) => (
              <tr key={`${group}-${value}`} className="border-b border-border/60">
                <td className="py-2 text-muted-foreground">{formatCoverageGroup(group)}</td>
                <td className="font-medium">{formatCoverageValue(value)}</td>
                <td>{fmtCount(stats.observations)}</td>
                <td>{fmtCount(stats.with_any_outcome)}</td>
                <td>{fmtPct(stats.outcome_coverage_pct)}</td>
                <td>{fmtPct(stats.h1_coverage_pct)}</td>
                <td>{fmtPct(stats.h3_coverage_pct)}</td>
                <td>{fmtPct(stats.h5_coverage_pct)}</td>
                <td>{fmtPct(stats.features_coverage_pct)}</td>
                <td>{fmtPct(stats.current_like_pct)}</td>
                <td>{fmtPct(stats.legacy_like_pct)}</td>
                <td>{stats.latest_trade_date || '-'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  )
}

function Recommendations({ rows }: { rows: AttributionRecommendation[] }) {
  const actionRows = rows.filter((row) => row.type !== 'policy_governor')
  if (!actionRows.length) {
    return <Panel title="策略建议"><p className="text-sm text-muted-foreground">暂无需要降权的信号。</p></Panel>
  }
  return (
    <Panel title="策略建议">
      <div className="space-y-2">
        {actionRows.map((row) => <RecommendationCard key={`${row.type}-${row.horizon}-${row.target}`} row={row} />)}
      </div>
    </Panel>
  )
}

function RecommendationCard({ row }: { row: AttributionRecommendation }) {
  const payload = parseRecommendationReason(row.reason)
  const evidence = payload.evidence || {}
  const target = formatPolicySignalTarget(row.target || payload.target, payload.scope)
  return (
    <div className="rounded-lg border border-border bg-muted/30 p-3">
      <div className="text-sm font-medium">
        {formatPolicyAction(row.type || payload.action)} · {target} · h={row.horizon}
      </div>
      <p className="mt-1 text-xs text-muted-foreground">
        权重 {payload.weight_multiplier ?? '-'} · 均收 {fmtPct(evidence.avg_return_pct)} · 胜率 {fmtPct(evidence.win_rate_pct)} · 回撤 {fmtPct(evidence.avg_drawdown_pct)}
      </p>
    </div>
  )
}

function PolicyGovernorBox({ governor }: { governor: PolicyGovernor | null }) {
  if (!governor) {
    return (
      <Panel title="策略治理器">
        <p className="text-sm text-muted-foreground">当前归因报告还没有治理器输出，等待下一次任务刷新。</p>
      </Panel>
    )
  }
  return (
    <Panel title="策略治理器">
      <div className="grid gap-3 md:grid-cols-4">
        <MetricCard label="治理状态" value={attributionGovernorStatusLabel(governor.status)} />
        <MetricCard label="建议模式" value={attributionModeRecommendationLabel(governor.mode_recommendation)} />
        <MetricCard label="下一步动作" value={attributionNextActionLabel(governor.next_action)} />
        <MetricCard label="晋级状态" value={attributionPromotionStatusLabel(governor.promotion_status)} />
        <MetricCard label="观察周期" value={`h=${governor.horizon || '-'}`} />
        <MetricCard label="自动切模式" value={governor.auto_apply ? '是' : '否'} />
      </div>
      <p className="mt-3 text-sm text-muted-foreground">{governor.summary || '-'}</p>
      <p className="mt-2 text-sm text-muted-foreground">{governor.next_action_summary || '-'}</p>
      <p className="mt-2 text-xs text-muted-foreground">
        说明：自动切模式=否 表示治理器不会自动把 dynamic policy 从 shadow 切到 on；
        下一步动作以中文卡片为准，底层 next_action 只用于排查追证据，不等于正式漏斗已经读取归因权重。
      </p>
      <PromotionChecklist rows={governor.promotion_checklist} />
      <ShadowGateLine gate={governor.shadow_gate} />
    </Panel>
  )
}

function PromotionChecklist({ rows }: { rows?: PromotionCheck[] }) {
  if (!rows?.length) {
    return <p className="mt-3 text-xs text-muted-foreground">晋级检查：暂无结构化检查项，等待下一次归因报告刷新。</p>
  }
  return (
    <div className="mt-4 grid gap-2 md:grid-cols-2">
      {rows.map((row) => (
        <div key={`${row.key}-${row.status}`} className="rounded-md border border-border/70 px-3 py-2 text-xs">
          <div className="flex items-center justify-between gap-2">
            <span className="font-medium">{formatPromotionCheckKey(row.key)}</span>
            <span className={promotionStatusClass(row.status)}>{formatPromotionCheckStatus(row.status)}</span>
          </div>
          <p className="mt-1 text-muted-foreground">{row.summary || '-'}</p>
        </div>
      ))}
    </div>
  )
}

function PolicyExecutionState({
  stats,
  governor,
  execution,
}: {
  stats: PolicyExecutionStats
  governor: PolicyGovernor | null
  execution: PolicyExecutionPayload | null
}) {
  const actionCount = execution?.signal_action_count ?? stats.actionCount
  const hasActions = actionCount > 0
  const scope = formatExecutionScope(execution?.scope || (hasActions ? 'funnel_shadow' : 'none'))
  const activeScope = formatExecutionActiveScope(execution, actionCount)
  const targetText = stats.targets.length ? stats.targets.join(' / ') : '-'
  const modeText = governor?.auto_apply
    ? '治理器允许自动晋级，但仍应通过运行时配置和人工复核留痕。'
    : '治理器不会自动把 dynamic policy 从 shadow 切到 on；下一步动作以中文状态为准，底层枚举只用于排查追证据。'
  const policyMode = execution?.funnel_dynamic_policy || '未知'
  const horizon = execution?.horizon || '-'
  const promotion = execution?.promotion_status || governor?.promotion_status
  const nextAction = attributionNextActionLabel(execution?.next_action || governor?.next_action)
  return (
    <Panel title="调权执行状态">
      <div className="grid gap-3 md:grid-cols-4">
        <MetricCard label="可执行调权" value={`${actionCount} 项`} />
        <MetricCard label="建议降权" value={`${stats.downCount} 项`} />
        <MetricCard label="建议升权" value={`${stats.upCount} 项`} />
        <MetricCard label="当前范围" value={`${scope} · h=${horizon}`} />
        <MetricCard label="实际生效" value={activeScope} />
        <MetricCard label="正式 dynamic" value={attributionFormalDynamicLabel(execution)} />
      </div>
      <p className="mt-3 text-sm text-muted-foreground">
        {attributionExecutionImpactText({ execution, actionCount, targetText })}
      </p>
      <p className="mt-2 text-xs text-muted-foreground">
        漏斗动态策略 {policyExecutionModeLabel(policyMode)}，晋级状态 {attributionPromotionStatusLabel(promotion)}，下一步 {nextAction}。{formatFormalDynamicReason(execution)}{modeText}
        Web 读盘室可通过 query_attribution 查看运营摘要、执行态和晋级检查；
        CLI 可通过 query_history(source=&quot;attribution&quot;) 查看 latest_source、remote_error、latest_operator_summary、next_action、latest_execution_state 和 latest_operations。
        {stats.otherCount > 0 ? ` 另有 ${stats.otherCount} 条非升降权建议保留为观察项。` : ''}
      </p>
    </Panel>
  )
}

function ShadowGateLine({ gate }: { gate?: JsonMap }) {
  if (!gate) return null
  return (
    <div className="mt-3 text-xs text-muted-foreground">
      Shadow 证据：run {fmtCount(gate.run_count)} · 新增匹配 {fmtCount(gate.added_matched)} · 移除匹配 {fmtCount(gate.removed_matched)}
      {' '}· 收益差 {fmtPct(gate.return_lift_pct)} · 胜率差 {fmtPct(gate.win_rate_lift_pct)} · 回撤差 {fmtPct(gate.drawdown_lift_pct)}
    </div>
  )
}

function CandidateShadowStats({ rows }: { rows: Array<{ horizon: string; grade: string; stats: MetricStats }> }) {
  if (!rows.length) {
    return <Panel title="候选影子分表现"><p className="text-sm text-muted-foreground">暂无候选影子分样本。</p></Panel>
  }
  return (
    <Panel title="候选影子分表现">
      <div className="overflow-auto">
        <table className="w-full min-w-[760px] text-left text-sm">
          <thead className="text-xs text-muted-foreground">
            <tr className="border-b border-border">
              <th className="py-2">周期</th>
              <th>评级</th>
              <th>样本</th>
              <th>均值</th>
              <th>中位数</th>
              <th>胜率</th>
              <th>大涨</th>
              <th>大跌</th>
              <th>平均回撤</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ horizon, grade, stats }) => (
              <tr key={`${horizon}-${grade}`} className="border-b border-border/60">
                <td className="py-2">{horizon}</td>
                <td className="font-medium">{formatGrade(grade)}</td>
                <td>{stats.count ?? 0}</td>
                <td className={financialValueClass(stats.avg_return_pct, '')}>{fmtPct(stats.avg_return_pct)}</td>
                <td className={financialValueClass(stats.median_return_pct, '')}>{fmtPct(stats.median_return_pct)}</td>
                <td>{fmtPct(stats.win_rate_pct)}</td>
                <td>{fmtPct(stats.big_win_rate_pct)}</td>
                <td>{fmtPct(stats.big_loss_rate_pct)}</td>
                <td className={financialValueClass(stats.avg_drawdown_pct, '')}>{fmtPct(stats.avg_drawdown_pct)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  )
}

function EntryQualityStats({ rows }: { rows: Array<{ horizon: string; grade: string; stats: MetricStats }> }) {
  if (!rows.length) {
    return <Panel title="入场质量表现"><p className="text-sm text-muted-foreground">暂无入场质量样本。</p></Panel>
  }
  return (
    <Panel title="入场质量表现">
      <div className="overflow-auto">
        <table className="w-full min-w-[760px] text-left text-sm">
          <thead className="text-xs text-muted-foreground">
            <tr className="border-b border-border">
              <th className="py-2">周期</th>
              <th>评级</th>
              <th>样本</th>
              <th>均值</th>
              <th>中位数</th>
              <th>胜率</th>
              <th>大涨</th>
              <th>大跌</th>
              <th>平均回撤</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ horizon, grade, stats }) => (
              <tr key={`${horizon}-${grade}`} className="border-b border-border/60">
                <td className="py-2">{horizon}</td>
                <td className="font-medium">{formatGrade(grade)}</td>
                <td>{stats.count ?? 0}</td>
                <td className={financialValueClass(stats.avg_return_pct, '')}>{fmtPct(stats.avg_return_pct)}</td>
                <td className={financialValueClass(stats.median_return_pct, '')}>{fmtPct(stats.median_return_pct)}</td>
                <td>{fmtPct(stats.win_rate_pct)}</td>
                <td>{fmtPct(stats.big_win_rate_pct)}</td>
                <td>{fmtPct(stats.big_loss_rate_pct)}</td>
                <td className={financialValueClass(stats.avg_drawdown_pct, '')}>{fmtPct(stats.avg_drawdown_pct)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  )
}

interface DataLineageRows {
  coverage: Array<{ horizon: string; grade: string; stats: MetricStats }>
  evidence: Array<{ horizon: string; evidence: string; stats: MetricStats }>
}

function DataLineageStats({ rows }: { rows: DataLineageRows }) {
  if (!rows.coverage.length && !rows.evidence.length) {
    return <Panel title="证据覆盖表现"><p className="text-sm text-muted-foreground">暂无证据覆盖样本。</p></Panel>
  }
  return (
    <Panel title="证据覆盖表现">
      <div className="grid gap-5 xl:grid-cols-2">
        <CoverageTable rows={rows.coverage} />
        <EvidenceTable rows={rows.evidence} />
      </div>
    </Panel>
  )
}

function CoverageTable({ rows }: { rows: DataLineageRows['coverage'] }) {
  return (
    <div className="overflow-auto">
      <div className="mb-2 text-xs font-medium text-muted-foreground">按覆盖等级</div>
      <table className="w-full min-w-[700px] text-left text-sm">
        <DataLineageTableHead label="覆盖" />
        <tbody>
          {rows.map(({ horizon, grade, stats }) => (
            <DataLineageRow key={`${horizon}-${grade}`} horizon={horizon} label={formatCoverageGrade(grade)} stats={stats} />
          ))}
        </tbody>
      </table>
    </div>
  )
}

function EvidenceTable({ rows }: { rows: DataLineageRows['evidence'] }) {
  return (
    <div className="overflow-auto">
      <div className="mb-2 text-xs font-medium text-muted-foreground">按证据项</div>
      <table className="w-full min-w-[700px] text-left text-sm">
        <DataLineageTableHead label="证据项" />
        <tbody>
          {rows.map(({ horizon, evidence, stats }) => (
            <DataLineageRow key={`${horizon}-${evidence}`} horizon={horizon} label={formatEvidenceKey(evidence)} stats={stats} />
          ))}
        </tbody>
      </table>
    </div>
  )
}

function DataLineageTableHead({ label }: { label: string }) {
  return (
    <thead className="text-xs text-muted-foreground">
      <tr className="border-b border-border">
        <th className="py-2">周期</th>
        <th>{label}</th>
        <th>样本</th>
        <th>均值</th>
        <th>胜率</th>
        <th>大涨</th>
        <th>大跌</th>
        <th>平均回撤</th>
      </tr>
    </thead>
  )
}

function DataLineageRow({ horizon, label, stats }: { horizon: string; label: string; stats: MetricStats }) {
  return (
    <tr className="border-b border-border/60">
      <td className="py-2">{horizon}</td>
      <td className="font-medium">{label}</td>
      <td>{stats.count ?? 0}</td>
      <td className={financialValueClass(stats.avg_return_pct, '')}>{fmtPct(stats.avg_return_pct)}</td>
      <td>{fmtPct(stats.win_rate_pct)}</td>
      <td>{fmtPct(stats.big_win_rate_pct)}</td>
      <td>{fmtPct(stats.big_loss_rate_pct)}</td>
      <td className={financialValueClass(stats.avg_drawdown_pct, '')}>{fmtPct(stats.avg_drawdown_pct)}</td>
    </tr>
  )
}

function SignalStats({ rows }: { rows: Array<{ horizon: string; signal: string; stats: MetricStats }> }) {
  return (
    <Panel title="信号表现">
      <div className="overflow-auto">
        <table className="w-full min-w-[760px] text-left text-sm">
          <thead className="text-xs text-muted-foreground">
            <tr className="border-b border-border">
              <th className="py-2">周期</th>
              <th>信号</th>
              <th>样本</th>
              <th>均值</th>
              <th>中位数</th>
              <th>胜率</th>
              <th>大涨</th>
              <th>大跌</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ horizon, signal, stats }) => (
              <tr key={`${horizon}-${signal}`} className="border-b border-border/60">
                <td className="py-2">{horizon}</td>
                <td className="font-medium">{signal}</td>
                <td>{stats.count ?? 0}</td>
                <td className={financialValueClass(stats.avg_return_pct, '')}>{fmtPct(stats.avg_return_pct)}</td>
                <td className={financialValueClass(stats.median_return_pct, '')}>{fmtPct(stats.median_return_pct)}</td>
                <td>{fmtPct(stats.win_rate_pct)}</td>
                <td>{fmtPct(stats.big_win_rate_pct)}</td>
                <td>{fmtPct(stats.big_loss_rate_pct)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  )
}

function OutcomeTables({ winners, losers }: { winners: StockOutcome[]; losers: StockOutcome[] }) {
  return (
    <div className="grid gap-6 xl:grid-cols-2">
      <OutcomeTable title="涨幅样本" rows={winners} />
      <OutcomeTable title="跌幅样本" rows={losers} />
    </div>
  )
}

function OutcomeTable({ title, rows }: { title: string; rows: StockOutcome[] }) {
  return (
    <Panel title={title}>
      <div className="space-y-2">
        {rows.slice(0, 12).map((row) => (
          <div key={`${row.trade_date}-${row.code}-${row.signal_type}`} className="grid grid-cols-[1fr_auto] gap-3 rounded-lg border border-border bg-background p-3">
            <div>
              <div className="text-sm font-medium">{row.code} {row.name || ''}</div>
              <div className="mt-1 text-xs text-muted-foreground">
                {row.trade_date} · {row.signal_type || '-'} · {row.track || '-'} · 影子分 {fmtScore(row.candidate_shadow_score)} · {formatGrade(row.candidate_shadow_grade)}
              </div>
              <div className="mt-1 text-xs text-muted-foreground">
                入场质量 {formatGrade(row.entry_quality_grade)} {fmtScore(row.entry_quality_score)}
                {row.entry_quality_risk_flags?.length ? ` · 风险 ${row.entry_quality_risk_flags.join('/')}` : ''}
              </div>
              <div className="mt-1 text-xs text-muted-foreground">
                证据 {formatCoverageGrade(row.data_lineage_coverage_grade)} {fmtScore(row.data_lineage_coverage_score)}
                {row.data_lineage_evidence_keys?.length ? ` · ${row.data_lineage_evidence_keys.map(formatEvidenceKey).join('/')}` : ''}
              </div>
              <div className="mt-1 text-xs text-muted-foreground">
                {formatCoverageValue(row.selection_mode || row.strategy_version || row.candidate_lane || row.entry_type || '-')}
              </div>
            </div>
            <div className={`text-right text-sm font-semibold ${financialValueClass(row.return_pct, '')}`}>{fmtPct(row.return_pct)}</div>
          </div>
        ))}
      </div>
    </Panel>
  )
}

function ShadowBox({ data }: { data: JsonMap }) {
  const latest = shadowLatest(data)
  const selection = latestSelection(latest)
  return (
    <Panel title="Shadow 差异">
      <div className="grid gap-3 md:grid-cols-3">
        <MetricCard label="记录数" value={String(data.count ?? 0)} />
        <MetricCard label="平均新增" value={String(data.avg_added ?? 0)} />
        <MetricCard label="平均移除" value={String(data.avg_removed ?? 0)} />
      </div>
      {latest ? (
        <p className="mt-3 text-xs text-muted-foreground">
          最新 {String(latest.trade_date || '-')}：新增 {fmtCount(selection?.diff_added_count)}，移除{' '}
          {fmtCount(selection?.diff_removed_count)}，新增样本 {formatCodeSample(latest.diff_added_sample)}。
        </p>
      ) : null}
    </Panel>
  )
}

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rounded-lg border border-border bg-card p-4">
      <h2 className="mb-3 text-base font-semibold">{title}</h2>
      {children}
    </section>
  )
}

function MetricCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-2 break-words text-sm font-semibold">{value}</div>
    </div>
  )
}

function LockedView() {
  return (
    <div className="h-full p-6">
      <div className="rounded-lg border border-border bg-card p-6">
        <h1 className="text-xl font-semibold">策略归因报告</h1>
        <p className="mt-2 text-sm text-muted-foreground">该视图仅对星球会员开放。</p>
      </div>
    </div>
  )
}

function EmptyView() {
  return (
    <div className="space-y-2 rounded-lg border border-border bg-card p-6 text-sm text-muted-foreground">
      <p>暂无远端策略归因报告。该页面只读取 Supabase `strategy_attribution_reports` 表。</p>
      <p>
        如果刚执行的是 `scripts/strategy_attribution_report.py --no-write`，报告只在本地文件中；
        请用 CLI/MCP 的 `query_history(source="attribution")` 查看本地只读结果。
      </p>
    </div>
  )
}

function ErrorBox({ message }: { message: string }) {
  return <div className="rounded-lg border border-destructive/50 bg-destructive/5 p-4 text-sm text-destructive">{message}</div>
}

function flattenObservationCoverage(data: JsonMap | undefined) {
  const raw = data?._observation_coverage
  if (!raw || typeof raw !== 'object') return []
  const groupOrder: Record<string, number> = {
    signal_type: 0,
    selection_mode: 1,
    strategy_version: 2,
    candidate_lane: 3,
    entry_type: 4,
  }
  return Object.entries(raw as Record<string, Record<string, ObservationCoverageMetric>>)
    .flatMap(([group, stats]) =>
      Object.entries(stats || {}).map(([value, item]) => ({ group, value, stats: item })),
    )
    .sort((a, b) => {
      const groupDiff = (groupOrder[a.group] ?? 99) - (groupOrder[b.group] ?? 99)
      if (groupDiff !== 0) return groupDiff
      return (b.stats.observations ?? 0) - (a.stats.observations ?? 0)
    })
}

function policyGovernor(data: JsonMap | undefined): PolicyGovernor | null {
  const raw = data?.policy_governor
  return raw && typeof raw === 'object' ? raw as PolicyGovernor : null
}

function policyExecutionPayload(data: JsonMap | undefined): PolicyExecutionPayload | null {
  const raw = data?.policy_execution_state
  return raw && typeof raw === 'object' ? raw as PolicyExecutionPayload : null
}

function policyOperationsPayload(data: JsonMap | undefined): PolicyOperationsPayload | null {
  const raw = data?.policy_operations_brief
  return raw && typeof raw === 'object' ? raw as PolicyOperationsPayload : null
}

function policyExecutionStats(rows: AttributionRecommendation[], horizon?: string): PolicyExecutionStats {
  const targets: string[] = []
  let downCount = 0
  let upCount = 0
  let otherCount = 0
  for (const row of rows) {
    if (row.type === 'policy_governor') continue
    const payload = parseRecommendationReason(row.reason)
    const rowHorizon = String(row.horizon || payload.horizon || '').trim()
    if (horizon && rowHorizon !== horizon) continue
    const action = String(row.type || payload.action || '').trim()
    const target = formatPolicySignalTarget(row.target || payload.target, payload.scope)
    if (target && !targets.includes(target)) targets.push(target)
    if (action === 'downweight') downCount += 1
    else if (action === 'upweight') upCount += 1
    else if (action) otherCount += 1
  }
  return {
    actionCount: downCount + upCount,
    downCount,
    upCount,
    otherCount,
    targets: targets.slice(0, 8),
  }
}

function formatExecutionScope(raw: string | undefined) {
  const labels: Record<string, string> = {
    none: '仅观察',
    funnel_shadow: '漏斗 shadow',
    funnel_formal: '正式漏斗',
  }
  return labels[String(raw || '').trim()] || raw || '仅观察'
}

function formatPolicySignalTarget(raw: unknown, scope?: Record<string, string>) {
  const signal = String(raw || '').trim()
  const parts = []
  const regime = String(scope?.regime || '').trim()
  const lane = String(scope?.lane || '').trim()
  const entry = String(scope?.entry_type || scope?.entry || '').trim()
  if (regime) parts.push(`regime=${regime}`)
  if (lane) parts.push(`lane=${lane}`)
  if (entry) parts.push(`entry=${entry}`)
  return parts.length ? `${signal}[${parts.join(', ')}]` : signal
}

function shadowLatest(data: JsonMap | undefined): JsonMap | null {
  const raw = data?.latest
  return raw && typeof raw === 'object' ? raw as JsonMap : null
}

function latestSelection(latest: JsonMap | null): JsonMap | null {
  const raw = latest?.selection_summary
  return raw && typeof raw === 'object' ? raw as JsonMap : null
}

function formatCodeSample(raw: unknown) {
  const rows = Array.isArray(raw) ? raw.map((x) => String(x || '').trim()).filter(Boolean) : []
  return rows.length ? rows.join(' / ') : '-'
}

function parseRecommendationReason(raw: string | undefined): PolicySignalAction {
  if (!raw) return {}
  try {
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? parsed as PolicySignalAction : {}
  } catch {
    return {}
  }
}

function flattenSignalStats(data: AttributionReport['signal_stats_json']) {
  return Object.entries(data || {}).flatMap(([horizon, stats]) =>
    Object.entries(stats || {}).map(([signal, item]) => ({ horizon, signal, stats: item })),
  )
}

function flattenCandidateShadowStats(data: JsonMap | undefined) {
  const raw = data?._candidate_shadow_grade
  if (!raw || typeof raw !== 'object') return []
  const gradeOrder: Record<string, number> = { S: 0, A: 1, B: 2, C: 3, D: 4, unknown: 5 }
  return Object.entries(raw as Record<string, Record<string, MetricStats>>)
    .flatMap(([horizon, stats]) =>
      Object.entries(stats || {}).map(([grade, item]) => ({ horizon, grade, stats: item })),
    )
    .sort((a, b) => {
      const horizonDiff = Number(a.horizon) - Number(b.horizon)
      if (Number.isFinite(horizonDiff) && horizonDiff !== 0) return horizonDiff
      return (gradeOrder[a.grade] ?? 99) - (gradeOrder[b.grade] ?? 99)
    })
}

function flattenEntryQualityStats(data: JsonMap | undefined) {
  const raw = data?._entry_quality_grade
  if (!raw || typeof raw !== 'object') return []
  const gradeOrder: Record<string, number> = { S: 0, A: 1, B: 2, C: 3, D: 4, unknown: 5 }
  return Object.entries(raw as Record<string, Record<string, MetricStats>>)
    .flatMap(([horizon, stats]) =>
      Object.entries(stats || {}).map(([grade, item]) => ({ horizon, grade, stats: item })),
    )
    .sort((a, b) => {
      const horizonDiff = Number(a.horizon) - Number(b.horizon)
      if (Number.isFinite(horizonDiff) && horizonDiff !== 0) return horizonDiff
      return (gradeOrder[a.grade] ?? 99) - (gradeOrder[b.grade] ?? 99)
    })
}

function flattenDataLineageStats(data: JsonMap | undefined) {
  const raw = data?._data_lineage
  if (!raw || typeof raw !== 'object') return { coverage: [], evidence: [] }
  const lineage = raw as JsonMap
  const coverageRaw = lineage.coverage_grade
  const evidenceRaw = lineage.evidence_key
  const coverageOrder: Record<string, number> = { strong: 0, medium: 1, thin: 2, weak: 3, unknown: 4 }
  const evidenceOrder: Record<string, number> = {
    daily_signal: 0,
    price_action: 1,
    springboard: 2,
    intraday_tail: 3,
    external_capital: 4,
    ai_review: 5,
  }
  const coverage = (!coverageRaw || typeof coverageRaw !== 'object' ? [] :
    Object.entries(coverageRaw as Record<string, Record<string, MetricStats>>)
      .flatMap(([horizon, stats]) =>
        Object.entries(stats || {}).map(([grade, item]) => ({ horizon, grade, stats: item })),
      ))
    .sort((a, b) => {
      const horizonDiff = Number(a.horizon) - Number(b.horizon)
      if (Number.isFinite(horizonDiff) && horizonDiff !== 0) return horizonDiff
      return (coverageOrder[a.grade] ?? 99) - (coverageOrder[b.grade] ?? 99)
    })
  const evidence = (!evidenceRaw || typeof evidenceRaw !== 'object' ? [] :
    Object.entries(evidenceRaw as Record<string, Record<string, MetricStats>>)
      .flatMap(([horizon, stats]) =>
        Object.entries(stats || {}).map(([evidence, item]) => ({ horizon, evidence, stats: item })),
      ))
    .sort((a, b) => {
      const horizonDiff = Number(a.horizon) - Number(b.horizon)
      if (Number.isFinite(horizonDiff) && horizonDiff !== 0) return horizonDiff
      return (evidenceOrder[a.evidence] ?? 99) - (evidenceOrder[b.evidence] ?? 99)
    })
  return { coverage, evidence }
}

function fmtPct(raw: unknown) {
  return typeof raw === 'number' && Number.isFinite(raw) ? `${raw.toFixed(1)}%` : '-'
}

function fmtScore(raw: number | null | undefined) {
  return typeof raw === 'number' && Number.isFinite(raw) ? raw.toFixed(1) : '-'
}

function fmtScoreNumber(raw: unknown) {
  return typeof raw === 'number' && Number.isFinite(raw) ? raw.toFixed(2) : '-'
}

function fmtCount(raw: unknown) {
  return typeof raw === 'number' && Number.isFinite(raw) ? String(raw) : '0'
}

function fmtWeight(raw: number | undefined) {
  return typeof raw === 'number' && Number.isFinite(raw) ? raw.toFixed(2) : '1.00'
}

function formatGrade(raw: string | null | undefined) {
  const text = String(raw || '').trim()
  return text && text !== 'unknown' ? text : '未评分'
}

function formatPolicyAction(raw: string | undefined) {
  const labels: Record<string, string> = {
    downweight: '建议降权',
    upweight: '建议升权',
    watch: '继续观察',
    hold: '保持权重',
  }
  return labels[String(raw || '').trim()] || raw || '策略建议'
}

function formatPromotionCheckKey(raw: string | undefined) {
  const labels: Record<string, string> = {
    shadow_sample: 'Shadow 样本',
    shadow_performance: 'Shadow 表现',
    selection_actions: '候选源治理',
    signal_actions: '信号调权',
    backtest_confirmation: '回测确认',
  }
  return labels[String(raw || '').trim()] || raw || '-'
}

function formatPromotionCheckStatus(raw: string | undefined) {
  const labels: Record<string, string> = {
    pass: '通过',
    fail: '未通过',
    review: '待复核',
    not_required: '无需',
  }
  return labels[String(raw || '').trim()] || raw || '-'
}

function formatExecutionActiveScope(execution: PolicyExecutionPayload | null, actionCount: number) {
  const explicit = String(execution?.active_scope || '').trim()
  if (explicit) return explicit
  const scope = String(execution?.scope || 'none').trim()
  if (actionCount <= 0) return '无'
  if (scope === 'funnel_formal') return '正式漏斗'
  if (scope === 'funnel_shadow') return '漏斗shadow'
  return '无'
}

function formatFormalDynamicReason(execution: PolicyExecutionPayload | null) {
  if (execution?.formal_dynamic_allowed !== false) return ''
  const reason = String(execution.formal_dynamic_block_reason || '').trim()
  return reason
    ? `正式 dynamic 被治理器拦截：${attributionFormalDynamicReasonLabel(reason)}。`
    : '正式 dynamic 被治理器拦截。'
}

function promotionStatusClass(raw: string | undefined) {
  const status = String(raw || '').trim()
  if (status === 'pass') return 'text-green-600 dark:text-green-400'
  if (status === 'fail') return 'text-red-600 dark:text-red-400'
  if (status === 'review') return 'text-amber-600 dark:text-amber-400'
  return 'text-muted-foreground'
}

function formatCoverageGrade(raw: string | null | undefined) {
  const text = String(raw || '').trim()
  const labels: Record<string, string> = {
    strong: '强覆盖',
    medium: '中覆盖',
    thin: '薄覆盖',
    weak: '弱覆盖',
  }
  return labels[text] || '未知覆盖'
}

function formatEvidenceKey(raw: string) {
  const labels: Record<string, string> = {
    daily_signal: '日线信号',
    price_action: '量价痕迹',
    springboard: '起跳板',
    intraday_tail: '尾盘确认',
    external_capital: '外部资金',
    ai_review: 'AI复核',
  }
  return labels[raw] || raw
}

function formatCoverageGroup(raw: string) {
  const labels: Record<string, string> = {
    signal_type: '信号',
    selection_mode: '选择模式',
    strategy_version: '策略版本',
    candidate_lane: '入选路径',
    entry_type: '买点类型',
  }
  return labels[raw] || raw
}

function formatCoverageValue(raw: string) {
  const labels: Record<string, string> = {
    candidate_lane_shadow: '入选路径观察',
    mainline_shadow: '主线观察',
    tradeable_l4: '正式买点',
    legacy_layered: '旧五层漏斗',
    candidate_lane_v1: '入选路径 v1',
    launchpad: '起跳板',
    trend_breakout: '趋势突破',
    trend_lane_pullback: '趋势回踩',
    mainline: '主线',
    main_force_entry: '主力入场',
    unknown: '未知/旧字段缺失',
  }
  return labels[raw] || raw
}

function formatDateTime(raw: string) {
  const date = new Date(raw)
  return Number.isNaN(date.getTime()) ? raw : date.toLocaleString()
}
