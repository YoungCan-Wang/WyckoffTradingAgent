/**
 * 策略归因页（只读）。
 *
 * 分区顺序照 workflows/strategy_attribution_report.py 的 build_report_markdown，
 * 但把「操作摘要」提到前面 —— core/prompts.py 明确要求先给 operator_summary，
 * next_action 之类的原始值只作证据，不直接复述给用户。
 *
 * 标签一律用后端算好的 policy_display / execution_summary，前端不重新推导：
 * 那套映射表在 core/strategy_policy_display.py，复制一份到前端必然会分叉。
 */
import { useIpc } from '../lib/useIpc'

const t = (key: string, params?: Record<string, string | number>) => window.WyckoffI18n.t(key, params)

interface PolicyDisplay {
  status?: string
  mode_recommendation?: string
  next_action?: string
  promotion_status?: string
  auto_apply?: string
}

interface ExecutionSummary {
  active_scope?: string
  promotion_status?: string
  next_action?: string
  formal_dynamic?: string
  summary?: string
}

interface SignalAction {
  action?: string
  horizon?: string | number
  label?: string
  weight_multiplier?: number
  scope?: string
}

interface AttributionRecord {
  report_date: string
  window_start: string
  window_end: string
  source: string
  shadow?: { runs?: number; avg_added?: number; avg_removed?: number }
  signal_actions?: SignalAction[]
}

interface AttributionData {
  total?: number
  records?: AttributionRecord[]
  latest_source?: string
  remote_error?: string
  latest_policy_display?: PolicyDisplay
  latest_execution_summary?: ExecutionSummary
  latest_operator_summary?: string
}

const DASH = '—'
const text = (value?: string) => (value && value.trim() ? value : DASH)

export function AttributionPage () {
  const { data, loading, failed } = useIpc<AttributionData>('attribution', { limit: 10 })

  if (loading) return <p className="empty">{t('tab.loading')}</p>
  if (failed) return <p className="empty">{t('attribution.readFailed')}</p>

  const records = (data && data.records) || []
  if (!data || !records.length) return <p className="empty">{t('attribution.empty')}</p>

  const latest = records[0]
  const policy = data.latest_policy_display || {}
  const execution = data.latest_execution_summary || {}
  const shadow = latest.shadow || {}
  const actions = latest.signal_actions || []
  const isLocal = (data.latest_source || latest.source) !== 'remote'

  return (
    <>
      {/* 数据来源必须显式说明：本地报告或云端读失败时，用户不该以为看的是最新云端结果 */}
      {isLocal || data.remote_error ? (
        <div className="attr-source">
          {isLocal ? t('attribution.localSource') : ''}
          {data.remote_error ? ` ${t('attribution.remoteError', { error: data.remote_error })}` : ''}
        </div>
      ) : null}

      <div className="attr-head">
        <b>{text(latest.report_date)}</b>
        <span>
          {t('attribution.window')} {text(latest.window_start)} ~ {text(latest.window_end)}
        </span>
      </div>

      {/* 提示词要求：先给这句人话摘要，再给拆解 */}
      {data.latest_operator_summary ? (
        <p className="attr-summary">{data.latest_operator_summary}</p>
      ) : null}

      <Section title={t('attribution.governance')}>
        <Field label={t('attribution.status')} value={text(policy.status)} />
        <Field label={t('attribution.modeRec')} value={text(policy.mode_recommendation)} />
        <Field label={t('attribution.promotion')} value={text(policy.promotion_status)} />
        <Field label={t('attribution.autoApply')} value={text(policy.auto_apply)} />
      </Section>

      <Section title={t('attribution.execution')}>
        <Field label={t('attribution.activeScope')} value={text(execution.active_scope)} />
        <Field label={t('attribution.formalDynamic')} value={text(execution.formal_dynamic)} />
        {execution.summary ? <p className="attr-note">{execution.summary}</p> : null}
      </Section>

      <Section title={t('attribution.shadow')}>
        <Field label={t('attribution.shadowRuns')} value={String(shadow.runs ?? DASH)} />
        <Field label={t('attribution.shadowAdded')} value={String(shadow.avg_added ?? DASH)} />
        <Field label={t('attribution.shadowRemoved')} value={String(shadow.avg_removed ?? DASH)} />
      </Section>

      <Section title={t('attribution.actions')}>
        {/* 空区块显式写「暂无」而不是省略 —— 与报告的行文风格一致 */}
        {actions.length === 0 ? (
          <p className="attr-note">{t('attribution.noActions')}</p>
        ) : (
          <div className="task-list">
            {actions.map((action, index) => (
              <div className="task-row" key={`${action.label || 'a'}-${index}`}>
                <div className="task-title">{text(action.label)}</div>
                <div className="task-meta">
                  {text(action.action)}
                  {action.horizon ? ` · h=${action.horizon}` : ''}
                  {typeof action.weight_multiplier === 'number' ? ` · ×${action.weight_multiplier}` : ''}
                  {action.scope ? ` · ${action.scope}` : ''}
                </div>
              </div>
            ))}
          </div>
        )}
      </Section>

      {records.length > 1 ? (
        <Section title={t('attribution.history')}>
          <div className="task-list">
            {records.slice(1).map((record) => (
              <div className="task-row" key={record.report_date}>
                <div className="task-title">{text(record.report_date)}</div>
                <div className="task-meta">
                  {text(record.window_start)} ~ {text(record.window_end)}
                  {record.source !== 'remote' ? ` · ${t('attribution.localTag')}` : ''}
                </div>
              </div>
            ))}
          </div>
        </Section>
      ) : null}
    </>
  )
}

function Section ({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="task-section">
      <div className="task-section-h">{title}</div>
      {children}
    </section>
  )
}

function Field ({ label, value }: { label: string; value: string }) {
  return (
    <div className="attr-field">
      <span>{label}</span>
      <b>{value}</b>
    </div>
  )
}
