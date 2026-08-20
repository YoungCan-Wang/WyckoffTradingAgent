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
import { useEffect, useRef, useState } from 'react'
import { collect } from '../lib/ipc'
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
  /** 摘要挂在 operations 下面，不在记录顶层。 */
  operations?: { operator_summary?: string }
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

/**
 * 动作文案。未知动作显示原值，不显示 i18n 键。
 *
 * i18n.t 缺 key 时返回键本身，所以后端加了新动作、前端还没翻译时，界面上会
 * 出现 `attribution.act.rebalance` 这种内部标识 —— 用户看到的是一串代码。
 * 同项目的 riskReasonText 对未知 key 就是返回空串的，这里补上同样的防护。
 */
const KNOWN_ACTIONS = new Set(['upweight', 'downweight'])
const actionText = (action?: string) => {
  const key = String(action || '').trim()
  if (!key) return DASH
  return KNOWN_ACTIONS.has(key) ? t(`attribution.act.${key}`) : key
}

/** 只有明确是加权才用上涨色；未知动作不该被涂成「减权」。 */
const actionClass = (action?: string) => {
  const key = String(action || '').trim()
  if (key === 'upweight') return 'aw-up'
  if (key === 'downweight') return 'aw-down'
  return 'aw-neutral'
}

interface DateEntry {
  report_date: string
  window_start: string
  window_end: string
}

interface DatesData {
  total?: number
  dates?: DateEntry[]
}

export function AttributionPage () {
  // 只拉日期列表（约 3 KB）。整份报告约 14 KB，20 份一起拉要 8 秒 —— 页签
  // 只用得到日期，正文按翻到哪一页再取哪一页。
  const { data: index, loading: indexLoading, failed: indexFailed } =
    useIpc<DatesData>('attribution_dates', { limit: 60 })

  const [picked, setPicked] = useState<string | null>(null)
  // 已取回的报告，按日期存。翻回看过的那页是瞬时的，不再请求。
  const [cache, setCache] = useState<Record<string, AttributionRecord>>({})
  const [loadError, setLoadError] = useState('')
  /**
   * 已经取过（或正在取）的日期。用 ref 而不是 state：它只用来去重，
   * 放进 state 会触发重渲染 → effect 依赖变化 → 取消刚发出的那次请求，
   * 结果永远停在「读取中」。
   */
  const requested = useRef<Set<string>>(new Set())
  const alive = useRef(true)
  useEffect(() => () => { alive.current = false }, [])
  /**
   * 当前选中的日期，供异步回调读取。
   *
   * 闭包里的 current 是发起那次请求时的值；晚归的失败要判断「我这个日期还是
   * 用户正在看的吗」，必须读一个**当下**的值，所以用 ref 而不是闭包变量。
   */
  const currentRef = useRef('')

  const dates = (index && index.dates) || []
  const current = picked || (dates.length ? dates[0].report_date : '')
  const record = current ? cache[current] : undefined

  /**
   * 翻到哪一页就取哪一页。
   *
   * 依赖只有 current 和 latestDate —— 刻意不放 cache：写缓存会改 state，
   * 若它在依赖里，effect 会重跑并清理掉刚发出的请求，页面永远停在「读取中」。
   * 去重靠 requested（ref，不参与渲染）。
   */
  const latestDate = dates.length ? dates[0].report_date : ''
  currentRef.current = current
  useEffect(() => {
    if (!current || requested.current.has(current)) return
    requested.current.add(current)
    setLoadError('')
    void (async () => {
      const asked = current
      const res = await collect('attribution', { report_date: asked }).catch(() => null)
      if (!alive.current) return
      const payload = res as AttributionData | null
      const got = payload && payload.records && payload.records[0]
      if (!got) {
        // 取失败就把标记撤掉，下次点这个页签可以重试。
        requested.current.delete(asked)
        // 但只有它仍然是当前选中的日期才显示错误 —— 否则「日期 A 请求失败晚归」
        // 会把错误显示在用户已经切过去的日期 B 头上，看起来是 B 坏了。
        if (asked === currentRef.current) setLoadError(t('attribution.readFailed'))
        return
      }
      setCache((prev) => ({ ...prev, [asked]: got }))
    })()
  }, [current, latestDate])

  if (indexLoading) return <p className="empty">{t('tab.loading')}</p>
  if (indexFailed) return <p className="empty">{t('attribution.readFailed')}</p>
  if (!dates.length) return <p className="empty">{t('attribution.empty')}</p>

  const meta = dates.find((d) => d.report_date === current)
  const policy = (record && record.policy_display) || {}
  const execution = (record && record.execution_summary) || {}
  const shadow = (record && record.shadow) || {}
  const actions = (record && record.signal_actions) || []
  const isLocal = Boolean(record) && record!.source !== 'remote'
  const remoteError = (record && record.remote_error) || ''
  // 提示词要求先给一句人话摘要，后端就是为此产出 operator_summary 的。
  // 原来这里完全没读它，直接用 policy/execution 拼一个替身 —— 类型里声明了、
  // 注释里也写了要用，但代码把真货丢了。真货缺失时才退回拼凑。
  const realSummary = String(
    (record && record.operations && record.operations.operator_summary) || ''
  ).trim()
  const operatorSummary = realSummary || [policy.status, policy.mode_recommendation, execution.summary]
    .filter((value, index, all) => Boolean(value) && all.indexOf(value) === index)
    .slice(0, 2)
    .join(' · ')

  return (
    <>
      {/*
        日期页签。日期列表单独取（约 3 KB），正文按翻到哪页取哪页并缓存 ——
        翻回看过的那页是瞬时的。横向可滚：换行会把页面顶部撑掉两三行。
      */}
      {dates.length > 1 ? (
        <div className="attr-tabs">
          {dates.map((entry) => (
            <button
              key={entry.report_date}
              type="button"
              className={entry.report_date === current ? 'attr-tab on' : 'attr-tab'}
              title={entry.report_date}
              aria-pressed={entry.report_date === current}
              onClick={() => setPicked(entry.report_date)}
            >
              {shortDate(entry.report_date)}
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

      {/* 头部用日期列表里的窗口信息 —— 正文还在路上时也能立刻显示 */}
      <div className="attr-head">
        <b>{text(current)}</b>
        <span>
          {t('attribution.window')} {text(meta && meta.window_start)} ~ {text(meta && meta.window_end)}
        </span>
      </div>

      {/*
        正文还在取：只显示一行「加载中」，不要把上一页的数字留在屏幕上 ——
        那会让人以为看的是当前这天的报告。
      */}
      {!record ? (
        <p className="empty">{loadError || t('tab.loading')}</p>
      ) : (
      <>
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
                        <span className={actionClass(action.action)}>
                          {actionText(action.action)}
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
      )}
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
