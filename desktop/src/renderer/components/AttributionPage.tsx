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
import { useState } from 'react'
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
  /**
   * 每份报告都自带治理与执行摘要 —— 所以历史报告能完整展示，而不只是一行日期。
   * 之前页面只读顶层的 latest_*，等于把已经拿到的历史数据丢掉了。
   */
  policy_display?: PolicyDisplay
  execution_summary?: ExecutionSummary
  operator_summary?: string
  remote_error?: string
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

/**
 * 页签上只显示「月-日」：20 个页签横排，年份重复且占宽。
 * 完整日期在下面的标题里，所以这里省掉不丢信息。
 */
const shortDate = (value?: string) => {
  const m = String(value || '').match(/^(\d{4})-(\d{2}-\d{2})$/)
  return m ? m[2] : text(value)
}

/** A 股惯例红涨绿跌；0 与缺失不着色。 */
const moveClass = (v: unknown) => {
  if (!isNum(v) || v === 0) return ''
  return v > 0 ? 'trk-up' : 'trk-down'
}

export function AttributionPage () {
  // 20 份约两个月。每份都带完整的治理/调权/shadow，所以切日期不用重新请求。
  const { data, loading, failed } = useIpc<AttributionData>('attribution', { limit: 20 })
  // 选中的报告日期。null = 还没选过，用最新那份。
  const [picked, setPicked] = useState<string | null>(null)

  if (loading) return <p className="empty">{t('tab.loading')}</p>
  if (failed) return <p className="empty">{t('attribution.readFailed')}</p>

  const records = (data && data.records) || []
  if (!data || !records.length) return <p className="empty">{t('attribution.empty')}</p>

  // 选中的那份可能已经不在列表里（重拉后日期变了），退回最新一份。
  const current = records.find((r) => r.report_date === picked) || records[0]
  // 全部读当前这份，而不是顶层的 latest_* —— 否则切到 8-15 时头部换了、
  // 下面的治理数字还是 8-19 的，这种「一半新一半旧」比读不到更危险。
  const policy = current.policy_display || {}
  const execution = current.execution_summary || {}
  const shadow = current.shadow || {}
  const actions = current.signal_actions || []
  const isLocal = current.source !== 'remote'
  const remoteError = current.remote_error || data.remote_error || ''
  // 摘要：记录自带的优先；只有看最新那份时才回退到顶层 latest_operator_summary
  // （后端只为最新一份算了它）。
  const isLatest = current.report_date === records[0].report_date
  const operatorSummary = current.operator_summary || (isLatest ? data.latest_operator_summary : '') || ''

  return (
    <>
      {/*
        日期页签。每份报告的数据早就在 records 里了，所以切换是纯本地的，不发请求。
        横向可滚：20 个日期放不进一行，但换行会把页面顶部撑掉两三行。
      */}
      {records.length > 1 ? (
        <div className="attr-tabs">
          {records.map((record) => (
            <button
              key={record.report_date}
              type="button"
              className={record.report_date === current.report_date ? 'attr-tab on' : 'attr-tab'}
              onClick={() => setPicked(record.report_date)}
            >
              {shortDate(record.report_date)}
              {/* 本地报告标一下，免得以为看的是云端结果 */}
              {record.source !== 'remote' ? <i>·</i> : null}
            </button>
          ))}
        </div>
      ) : null}

      {/* 数据来源必须显式说明：本地报告或云端读失败时，用户不该以为看的是最新云端结果 */}
      {isLocal || remoteError ? (
        <div className="attr-source">
          {isLocal ? t('attribution.localSource') : ''}
          {remoteError ? ` ${t('attribution.remoteError', { error: remoteError })}` : ''}
        </div>
      ) : null}

      <div className="attr-head">
        <b>{text(current.report_date)}</b>
        <span>
          {t('attribution.window')} {text(current.window_start)} ~ {text(current.window_end)}
        </span>
      </div>

      {/* 提示词要求：先给这句人话摘要，再给拆解 */}
      {operatorSummary ? (
        <p className="attr-summary">{operatorSummary}</p>
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
