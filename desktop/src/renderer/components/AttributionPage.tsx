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
  target?: string
  weight_multiplier?: number
  /**
   * scope 是对象而不是字符串（实测始终是空 {}）。以前直接插进模板串，
   * 于是界面上出现一串 [object Object] —— 现在不显示它，改显示 evidence
   * 里的样本数与收益，那才是「为什么要调这个权重」的依据。
   */
  scope?: Record<string, unknown>
  evidence?: {
    count?: number
    avg_return_pct?: number
    win_rate_pct?: number
    big_loss_rate_pct?: number
    avg_drawdown_pct?: number
  }
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

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const numText = (v: unknown, digits = 2) => (isNum(v) ? v.toFixed(digits) : DASH)
const pctText = (v: unknown) => (isNum(v) ? `${v > 0 ? '+' : ''}${v.toFixed(2)}%` : DASH)
/** 胜率这类比例：没有「正负」的含义，所以不带符号。 */
const rateText = (v: unknown) => (isNum(v) ? `${v.toFixed(1)}%` : DASH)

/** A 股惯例红涨绿跌；0 与缺失不着色。 */
const moveClass = (v: unknown) => {
  if (!isNum(v) || v === 0) return ''
  return v > 0 ? 'trk-up' : 'trk-down'
}

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
          /* 一张小表而不是卡片列表：12 条同构的记录，对齐了才比得出哪个信号更差。 */
          <div className="attr-tbl">
            <table>
              <tbody>
                <tr>
                  <th>{t('attribution.signal')}</th>
                  <th>{t('attribution.actionCol')}</th>
                  <th className="r">{t('attribution.weight')}</th>
                  <th className="r">{t('attribution.samples')}</th>
                  <th className="r">{t('attribution.avgReturn')}</th>
                  <th className="r">{t('attribution.winRate')}</th>
                  <th className="r">{t('attribution.drawdown')}</th>
                </tr>
                {actions.map((action, index) => {
                  const ev = action.evidence || {}
                  return (
                    <tr key={`${action.label || 'a'}-${index}`}>
                      <td>
                        <code>{text(action.label)}</code>
                        {action.horizon ? <span className="dim"> h={action.horizon}</span> : null}
                      </td>
                      <td>
                        <span className={action.action === 'upweight' ? 'aw-up' : 'aw-down'}>
                          {t(`attribution.act.${action.action}`)}
                        </span>
                      </td>
                      <td className="r">
                        {typeof action.weight_multiplier === 'number' ? `×${action.weight_multiplier}` : DASH}
                      </td>
                      <td className="r">{numText(ev.count, 0)}</td>
                      {/* 平均收益与回撤按 A 股惯例红涨绿跌着色 */}
                      <td className={`r ${moveClass(ev.avg_return_pct)}`}>{pctText(ev.avg_return_pct)}</td>
                      {/* 胜率是比例不是涨跌：不带正号，一位小数就够 */}
                      <td className="r">{rateText(ev.win_rate_pct)}</td>
                      <td className={`r ${moveClass(ev.avg_drawdown_pct)}`}>{pctText(ev.avg_drawdown_pct)}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      {records.length > 1 ? (
        <Section title={t('attribution.history')}>
          {/* 不用 .task-row：那个类是三列网格且首列留给状态点，没有点时
              标题会被挤进 10px 的轨道里跟副文字叠在一起。 */}
          <div className="attr-hist">
            {records.slice(1).map((record) => (
              <div className="attr-hist-row" key={record.report_date}>
                <b>{text(record.report_date)}</b>
                <span>
                  {text(record.window_start)} ~ {text(record.window_end)}
                  {record.source !== 'remote' ? ` · ${t('attribution.localTag')}` : ''}
                </span>
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
