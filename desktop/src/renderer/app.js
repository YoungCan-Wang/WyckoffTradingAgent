'use strict'

// i18n shorthand. resolveInitial picks user choice > system language > zh, and
// applyDom must run before first paint so the static HTML is not briefly zh.
const i18n = window.WyckoffI18n
const t = (key, params) => i18n.t(key, params)
i18n.setLang(i18n.resolveInitial(), { persist: false })

const stream = document.getElementById('stream')
const input = document.getElementById('input')
const btnSend = document.getElementById('btn-send')
const btnRestart = document.getElementById('btn-restart')
const paneBody = document.getElementById('pane-body')
const win = document.querySelector('.win')

let ready = false
let busy = false
const turns = new Map()

// Panels start closed so the conversation owns the window, as in ChatGPT and
// Claude desktop. Sidebar choice persists; the artifact pane does not — it is
// opened by content, and a stale empty pane on launch is just dead space.
const SIDE_KEY = 'wyckoff.sidebar'

// A sidebar or window change shifts the pane horizontally without resizing the
// placeholder, so ResizeObserver never fires. Re-sync explicitly.
window.addEventListener('resize', () => syncBrowserBounds())

function setSide (on) {
  win.classList.toggle('side-off', !on)
  document.getElementById('btn-side').classList.toggle('on', on)
  try { localStorage.setItem(SIDE_KEY, on ? '1' : '0') } catch { /* private mode */ }
  // The sidebar animates; re-sync after the transition settles.
  setTimeout(syncBrowserBounds, 220)
}

function setPane (on) {
  win.classList.toggle('pane-on', Boolean(on))
  // The browser is a native view floating above the DOM; hiding the pane must
  // detach it or it stays visible over the conversation.
  if (!on) window.wyckoff.browser.hide()
}

setSide((() => {
  try { return localStorage.getItem(SIDE_KEY) === '1' } catch { return false }
})())
setPane(false)

const el = (tag, cls, text) => {
  const node = document.createElement(tag)
  if (cls) node.className = cls
  if (text !== undefined) node.textContent = text
  return node
}

const atBottom = () => stream.scrollHeight - stream.scrollTop - stream.clientHeight < 60

// Messages go into .inner, which is width-capped and centred; #stream itself
// is the scroll container. Appending straight to #stream would bypass that cap.
const streamInner = document.getElementById('stream-inner')
const thread = document.querySelector('.thread')
// Declared before append() runs: it calls enterChat() on the first message.
let chatting = false

function append (node) {
  const stick = atBottom()
  // Any real content ends the welcome state, whichever path produced it.
  enterChat()
  streamInner.appendChild(node)
  if (stick) stream.scrollTop = stream.scrollHeight
  return node
}

function sysLine (text, isError) {
  append(el('div', isError ? 'sys err' : 'sys', text))
}

function userBubble (text) {
  const msg = el('div', 'msg')
  msg.appendChild(el('span', 'av', t('chat.you')))
  const bd = el('div', 'bd')
  bd.appendChild(el('p', null, text))
  msg.appendChild(bd)
  append(msg)
}

function newTurn (id) {
  const msg = el('div', 'msg a')
  msg.appendChild(el('span', 'av', '✳'))
  const bd = el('div', 'bd')
  msg.appendChild(bd)
  append(msg)
  const turn = { bd, think: null, text: null }
  turns.set(id, turn)
  return turn
}

function renderEvent (event) {
  const turn = turns.get(event.id)
  if (!turn) return

  switch (event.type) {
    case 'thinking_delta':
      if (!turn.think) turn.think = turn.bd.appendChild(el('div', 'think'))
      turn.think.textContent += event.text || ''
      break

    case 'text_delta':
      if (!turn.text) turn.text = turn.bd.appendChild(el('p'))
      turn.text.textContent += event.text || ''
      break

    case 'tool_start': {
      const row = el('div', 'tool')
      row.appendChild(el('span', 'g', '◈'))
      row.appendChild(el('span', 'nm', event.display_name || event.name || 'tool'))
      turn.bd.appendChild(row)
      // Drawing on a chart is only useful if the chart is visible, so surface it
      // in the pane. The turn's `end` handler refreshes it once drawing finishes.
      if (event.name === 'annotate_chart') {
        const code = event.args && event.args.code
        if (code) {
          // Remember it so `end` only refetches when drawing actually happened —
          // a blind refresh on every turn would re-pull 320 bars each message.
          turn.drewCharts = turn.drewCharts || new Set()
          turn.drewCharts.add(String(code))
          openKline(String(code))
          turn.bd.appendChild(openedNote(`${code} ${t('kline.title')}`))
        }
      }
      break
    }

    case 'tool_error':
      turn.bd.appendChild(el('div', 'sys err', `${event.name || t('chat.tool')}：${event.error || t('chat.toolFailed')}`))
      break

    case 'approval_pending':
      turn.bd.appendChild(approvalCard(event))
      refreshApprovals()
      break

    case 'error':
      turn.bd.appendChild(el('div', 'sys err', event.message || t('chat.errored')))
      break

    case 'done': {
      const body = event.text || (turn.text ? turn.text.textContent : '')
      if (!turn.text && event.text) {
        turn.bd.appendChild(el('p', null, event.text))
      }
      // A report-shaped reply belongs in the artifact pane, not the chat column.
      if (looksLikeReport(body)) {
        const title = reportTitle(body)
        openReport(title, body, new Date().toLocaleString('zh-CN'))
        turn.bd.appendChild(openedNote(title))
        if (turn.text) turn.text.remove()
      }
      break
    }

    case 'end':
      turns.delete(event.id)
      setBusy(false)
      // Only refetch charts this turn actually drew on — the annotation was
      // written after the tab first built, so it needs one more pass.
      refreshDrawnCharts(turn.drewCharts)
      break
  }
  if (atBottom()) stream.scrollTop = stream.scrollHeight
}

/** Long, structured replies read better as a document than as a chat bubble. */
function looksLikeReport (text) {
  if (!text || text.length < 400) return false
  const hasHeading = /^#{1,3}\s+\S/m.test(text)
  const hasTable = /^\|.*\|$/m.test(text)
  return hasHeading || hasTable
}

function reportTitle (text) {
  const heading = text.match(/^#\s+(.+)$/m) || text.match(/^##\s+(.+)$/m)
  const raw = heading ? heading[1].trim() : t('chat.report')
  return raw.length > 18 ? `${raw.slice(0, 18)}…` : raw
}

function openedNote (title) {
  const note = el('div', 'sys')
  note.textContent = t('chat.openedRight', { title })
  return note
}

function displayTime (value) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString(i18n.getLang())
}

function evidenceItem (label, value) {
  const item = el('div', 'evidence-item')
  item.appendChild(el('span', null, label))
  item.appendChild(el('b', null, value || '—'))
  return item
}

/**
 * 「为什么需要你确认」——把后端算好的档位理由翻成一句话。
 *
 * 后端存的是 i18n key 而不是成句文本，所以中英切换不会漏出另一种语言。
 * 认不出的 key 一律返回空串：宁可不解释，也不要显示 'reason.foo' 这种内部标识。
 */
function riskReasonText (item) {
  const key = String(item.risk_reason || '')
  if (!key.startsWith('reason.')) return ''
  const name = key.slice('reason.'.length)
  const known = ['destructive_action', 'over_nav', 'batch_over_nav', 'batch_malformed',
    'nav_unknown', 'write_tool', 'auto_narrow_tool']
  if (!known.includes(name)) return ''
  const ratio = Number(item.nav_ratio) || 0
  // 占比只在与阈值相关的理由里才有意义，别给「清仓」硬贴一个百分比。
  if ((name === 'over_nav' || name === 'batch_over_nav') && ratio > 0) {
    return t(`approvals.reason.${name}`, { pct: (ratio * 100).toFixed(1) })
  }
  return t(`approvals.reason.${name}`)
}

function approvalEvidence (item, context) {
  const grid = el('div', 'approval-evidence')
  if (context.account) grid.appendChild(evidenceItem(t('approvals.account'), context.account))
  grid.appendChild(evidenceItem(t('approvals.source'), item.source || t('tasks.sourceApproval')))
  grid.appendChild(evidenceItem(t('approvals.requestedAt'), displayTime(item.created_at)))
  grid.appendChild(evidenceItem(t('approvals.tool'), item.tool_name || item.tool))
  if (item.schedule_id) grid.appendChild(evidenceItem(t('approvals.schedule'), item.schedule_id))
  return grid
}

function approvalCard (item, context = {}) {
  const card = el('div', 'card')
  const r1 = el('div', 'r1')
  r1.appendChild(el('b', null, item.summary || item.tool || item.tool_name || t('approvals.defaultItem')))
  const tier = { confirm: t('approvals.tierConfirm'), review: t('approvals.tierReview') }[item.risk] || item.risk || ''
  const tone = ['confirm', 'review'].includes(item.risk) ? item.risk : ''
  r1.appendChild(el('span', `tg ${tone}`, tier))
  card.appendChild(r1)
  const why = riskReasonText(item)
  if (why) card.appendChild(el('p', 'approval-why', why))
  card.appendChild(el('p', 'sub', t('approvals.submitted')))
  if (item.created_at || item.source || item.tool_name || context.account) {
    card.appendChild(approvalEvidence(item, context))
  }
  if (item.args && Object.keys(item.args).length) {
    const details = el('details', 'approval-args')
    details.appendChild(el('summary', null, t('approvals.exactChange')))
    details.appendChild(el('pre', null, JSON.stringify(item.args, null, 2)))
    card.appendChild(details)
  }

  const btns = el('div', 'btns')
  const ok = el('button', 'b pri', t('action.approve'))
  const no = el('button', 'b', t('action.reject'))
  btns.append(ok, no)
  card.appendChild(btns)

  const decide = async (approved) => {
    ok.disabled = no.disabled = true
    const res = await window.wyckoff.call('approve_decide', { id: item.id, approved })
    if (!res.ok) {
      card.appendChild(el('div', 'sys err', res.error || t('approvals.callFailed')))
      return
    }
    const off = window.wyckoff.onEvent((event) => {
      if (event.id !== res.id) return
      if (event.type === 'result') {
        const label = event.status === 'executed' ? t('approvals.executed')
          : event.status === 'failed' ? t('approvals.failed') : t('approvals.rejected')
        card.appendChild(el('div', event.status === 'failed' ? 'sys err' : 'sys', label))
        refreshApprovals()
      } else if (event.type === 'error') {
        card.appendChild(el('div', 'sys err', event.message || t('chat.toolFailed')))
      } else if (event.type === 'end') {
        off()
      }
    })
  }
  ok.onclick = () => decide(true)
  no.onclick = () => decide(false)
  return card
}

function setBusy (value) {
  busy = value
  btnSend.disabled = value || !ready
}

// Backend-error empty state: one plain-language line + a retry button, shown
// centered over the stream. reason maps to localized copy; unknown reasons fall
// back to a generic message so a new bridge reason never leaks raw text.
const BACKEND_REASONS = new Set(['spawn_failed', 'exited_early', 'gave_up', 'timeout'])

function showBackendError (reason) {
  const key = BACKEND_REASONS.has(reason) ? reason : 'exited_early'
  let box = document.getElementById('backend-error')
  if (!box) {
    box = el('div', 'berr')
    box.id = 'backend-error'
    stream.appendChild(box)
  }
  box.replaceChildren(
    el('div', 'berr-t', t(`backendError.${key}.title`)),
    el('div', 'berr-s', t(`backendError.${key}.sub`))
  )
  const retry = el('button', 'berr-b', t('action.retry'))
  retry.onclick = () => { clearBackendError(); window.wyckoff.restart() }
  box.appendChild(retry)
  document.getElementById('welcome').hidden = true
  streamInner.hidden = true
  box.hidden = false
}

function clearBackendError () {
  const box = document.getElementById('backend-error')
  if (box) box.hidden = true
  streamInner.hidden = false
  // Welcome reappears only if the conversation has not started.
  if (!chatting) document.getElementById('welcome').hidden = false
}

function setStatus (payload) {
  if (payload.state === 'log') return

  const wasReady = ready
  ready = payload.state === 'ready'
  btnSend.disabled = !ready || busy

  // A healthy local backend is the default expectation, so we never advertise
  // it. The restart button (with a coloured dot) surfaces ONLY when the process
  // is not ready — that is the only moment the user can act on it.
  btnRestart.hidden = ready
  btnRestart.className = ready ? 'icb' : `icb has-dot ${payload.state}`

  if (payload.state === 'ready') {
    clearBackendError()
    refreshApprovals()
    refreshSchedules()
    // Only on the transition into ready, and only before the conversation
    // starts, so a restart mid-conversation does not disturb the transcript.
    loadAccount()
    loadAppearance()
    if (!wasReady && !chatting) loadWelcome()
  } else if (payload.state === 'error') {
    // A calm empty state with a retry action — never raw diagnostics. The
    // technical detail already went to the logs from the main process.
    showBackendError(payload.reason)
  }
  // starting / restarting / stopped stay silent: the header status dot already
  // reflects them, and auto-restart is in flight.
}

async function send () {
  const text = input.value.trim()
  if (!text || busy || !ready) return
  input.value = ''
  input.style.height = 'auto'
  userBubble(text)
  setBusy(true)

  const res = await window.wyckoff.call('chat', { text })
  if (!res.ok) {
    sysLine(res.error || t('chat.sendFailed'), true)
    setBusy(false)
    return
  }
  newTurn(res.id)
}

async function collect (method, params) {
  const res = await window.wyckoff.call(method, params)
  if (!res.ok) return null
  return new Promise((resolve) => {
    let payload = null
    const off = window.wyckoff.onEvent((event) => {
      if (event.id !== res.id) return
      if (event.type === 'result') payload = event
      if (event.type === 'end') {
        off()
        resolve(payload)
      }
    })
  })
}

/**
 * 与 collect 同样等一轮结束，但把 error 事件带回来而不是当成空结果。
 *
 * collect 在失败时返回 null，调用方分不清「成功但无数据」和「被拒绝」。
 * 对重跑这类会改变状态的调用，把 already_running 显示成「完成」是错的。
 */
function callWithError (method, params) {
  return new Promise((resolve, reject) => {
    window.wyckoff.call(method, params).then((res) => {
      if (!res.ok) return reject(new Error(res.error || 'call failed'))
      let payload = null
      let failure = null
      const off = window.wyckoff.onEvent((event) => {
        if (event.id !== res.id) return
        if (event.type === 'result') payload = event
        if (event.type === 'error') failure = new Error(event.message || event.code || 'failed')
        if (event.type === 'end') {
          off()
          failure ? reject(failure) : resolve(payload)
        }
      })
    }, reject)
  })
}

async function refreshApprovals () {
  const data = await collect('approve_list')
  const badge = document.getElementById('approval-count')
  const count = data ? data.count : 0
  badge.textContent = count ? String(count) : ''
  badge.className = count ? 'n warn' : 'n'

  // Pending approvals are money decisions; with the sidebar closed by default
  // that badge would be invisible. Surface it in the always-visible header.
  const chip = document.getElementById('pending-chip')
  chip.textContent = count ? t('chat.pendingCount', { count }) : ''
  chip.hidden = !count
}

async function refreshSchedules () {
  const data = await collect('schedules')
  if (!data) return
  const count = (data.schedules || []).length
  document.getElementById('schedule-count').textContent = count ? String(count) : ''
  document.getElementById('task-count').textContent = count ? String(count) : ''
}

// The pane reports how many tabs it holds, so visibility follows content:
// it appears when something opens and disappears when the last tab closes.
// Every open path gets this for free instead of toggling at each call site.
const pane = new window.WyckoffTabs.TabPane('tabs', 'pane-body', {
  onCountChange: (count) => setPane(count > 0)
})

function buildApprovals (data, account) {
  const wrap = el('div')
  if (!data || !data.count) {
    wrap.appendChild(el('p', 'empty', t('approvals.empty')))
    return wrap
  }
  const accountLabel = account ? (account.signed_in ? account.email : t('account.signedOut')) : null
  for (const item of data.items) wrap.appendChild(approvalCard(item, { account: accountLabel }))
  return wrap
}

const failedStatus = (value) => /fail|error|失败|异常/i.test(String(value || ''))
const successStatus = (value) => /success|complete|done|ok|成功|完成/i.test(String(value || ''))

function scheduleState (schedule) {
  if (!schedule.enabled) return { tone: '', label: t('tasks.statusDisabled') }
  if (failedStatus(schedule.last_status) || schedule.last_error) return { tone: 'failed', label: t('tasks.statusFailed') }
  if (successStatus(schedule.last_status)) return { tone: 'success', label: t('tasks.statusSuccess') }
  return { tone: '', label: t('tasks.statusEnabled') }
}

function describeCron (cron) {
  const parts = String(cron || '').trim().split(/\s+/)
  if (parts.length !== 5) return t('schedules.rawCron', { cron })
  const [minute, hour, , , weekday] = parts
  if (/^\*\/\d+$/.test(minute) && hour === '*') {
    return t('schedules.everyMinutes', { count: minute.slice(2) })
  }
  if (/^\d+$/.test(minute) && /^\d+$/.test(hour)) {
    const time = `${hour.padStart(2, '0')}:${minute.padStart(2, '0')}`
    if (weekday === '1-5') return t('schedules.everyWeekday', { time })
    if (weekday === '*') return t('schedules.everyDay', { time })
  }
  return t('schedules.rawCron', { cron })
}

function taskMetric (value, label) {
  const card = el('div', 'task-metric')
  card.appendChild(el('b', 'tnum', String(value)))
  card.appendChild(el('span', null, label))
  return card
}

function taskRow ({ tone, title, meta, state, time, action, onAction }) {
  const row = el('div', 'task-row')
  row.appendChild(el('span', `task-dot ${tone || ''}`))
  const body = el('div')
  body.appendChild(el('div', 'task-title', title))
  body.appendChild(el('div', 'task-meta', meta))
  row.appendChild(body)
  const side = el('div', 'task-side')
  side.appendChild(el('span', 'task-state', state))
  side.appendChild(el('span', 'task-time', time || '—'))
  if (action) {
    const button = el('button', 'task-action', action)
    button.onclick = onAction
    side.appendChild(button)
  }
  row.appendChild(side)
  return row
}

function taskSection (title, count) {
  const section = el('section', 'task-section')
  const head = el('div', 'task-section-h')
  head.appendChild(el('h2', null, title))
  head.appendChild(el('span', 'tnum', String(count)))
  section.appendChild(head)
  const list = el('div', 'task-list')
  section.appendChild(list)
  return { section, list }
}

function buildTasks (approvals, data) {
  const wrap = el('div')
  const schedules = (data && data.schedules) || []
  const pending = (approvals && approvals.items) || []
  const enabled = schedules.filter((item) => item.enabled)
  const issues = schedules.filter((item) => item.enabled && (failedStatus(item.last_status) || item.last_error))
  const metrics = el('div', 'task-metrics')
  metrics.append(
    taskMetric(enabled.length, t('tasks.enabled')),
    taskMetric(pending.length, t('tasks.pending')),
    taskMetric(issues.length, t('tasks.issues')),
    taskMetric(data && data.daemon_running ? t('tasks.running') : t('tasks.stopped'), t('tasks.runtime'))
  )
  wrap.appendChild(metrics)

  const attention = taskSection(t('tasks.attention'), pending.length + issues.length)
  if (!pending.length && !issues.length) attention.list.appendChild(el('p', 'empty', t('tasks.noneAttention')))
  for (const item of pending) attention.list.appendChild(approvalTaskRow(item))
  for (const item of issues) attention.list.appendChild(scheduleTaskRow(item, true))
  wrap.appendChild(attention.section)

  const scheduled = taskSection(t('tasks.scheduled'), schedules.length)
  if (!schedules.length) scheduled.list.appendChild(el('p', 'empty', t('tasks.noneScheduled')))
  for (const item of schedules) scheduled.list.appendChild(scheduleTaskRow(item, false))
  wrap.appendChild(scheduled.section)
  return wrap
}

function approvalTaskRow (item) {
  const meta = [t('tasks.sourceApproval'), item.tool_name, item.source].filter(Boolean).join(' · ')
  return taskRow({
    tone: 'pending', title: item.summary || item.tool_name || t('approvals.defaultItem'), meta,
    state: t('tasks.statusPending'), time: displayTime(item.created_at),
    action: t('tasks.openApprovals'), onAction: () => navigateView('approvals')
  })
}

function scheduleTaskRow (schedule, issue) {
  const state = scheduleState(schedule)
  const last = schedule.last_fired ? t('tasks.lastRun', { time: displayTime(schedule.last_fired) }) : t('tasks.neverRun')
  return taskRow({
    tone: issue ? 'failed' : state.tone, title: schedule.name, meta: `${t('tasks.sourceSchedule')} · ${describeCron(schedule.cron)} · ${last}`,
    state: state.label, time: schedule.next_run ? t('tasks.nextRun', { time: displayTime(schedule.next_run) }) : '—',
    action: t('tasks.openSchedules'), onAction: () => navigateView('schedules')
  })
}

function buildScheduleCard (schedule) {
  const card = el('div', 'schedule-card')
  const identity = el('div')
  identity.appendChild(el('div', 'schedule-name', schedule.name))
  identity.appendChild(el('div', 'schedule-cadence', `${describeCron(schedule.cron)} · ${schedule.cron}`))
  const last = el('div')
  last.appendChild(el('div', 'schedule-label', t('schedules.lastRun')))
  const state = scheduleState(schedule)
  const lastValue = !schedule.enabled
    ? state.label
    : schedule.last_fired ? `${displayTime(schedule.last_fired)} · ${state.label}` : t('schedules.neverRun')
  last.appendChild(el('div', `schedule-value ${state.tone}`, schedule.last_error || lastValue))
  const next = el('div')
  next.appendChild(el('div', 'schedule-label', t('schedules.nextRun')))
  next.appendChild(el('div', 'schedule-value tnum', schedule.next_run ? displayTime(schedule.next_run) : '—'))
  card.append(identity, last, next, rerunButton(schedule, card))
  return card
}

/**
 * 手动重跑。只在配了动作时给按钮 —— 没有 action 的任务点了必然报错。
 *
 * 重跑要跑完整一轮 agent（可能几分钟），所以按钮立刻禁用并就地显示进度，
 * 而不是把用户丢去别的视图猜有没有在跑。
 */
function rerunButton (schedule, card) {
  const button = el('button', 'schedule-rerun', t('schedules.rerun'))
  button.type = 'button'
  if (!schedule.id) button.disabled = true
  button.onclick = async () => {
    button.disabled = true
    button.textContent = t('schedules.rerunning')
    const note = el('p', 'schedule-rerun-note', t('schedules.rerunStarted', { name: schedule.name }))
    const old = card.querySelector('.schedule-rerun-note')
    if (old) old.remove()
    card.appendChild(note)
    try {
      const res = await callWithError('schedule_run', { id: schedule.id })
      // ok=false 是「跑完了但失败」，与传输层异常不同，两者都要说清楚。
      if (res && res.ok === false) {
        note.className = 'schedule-rerun-note failed'
        note.textContent = t('schedules.rerunFailed', { error: res.error || '' })
      } else {
        const queued = (res && res.queued) || []
        note.textContent = queued.length
          ? t('schedules.rerunQueued', { count: queued.length })
          : t('schedules.rerunDone')
      }
    } catch (err) {
      note.className = 'schedule-rerun-note failed'
      note.textContent = t('schedules.rerunFailed', { error: (err && err.message) || String(err) })
    }
    button.disabled = false
    button.textContent = t('schedules.rerun')
    // 重跑可能产生新审批，侧栏计数要跟上。
    refreshApprovals()
  }
  return button
}

function buildSchedules (data) {
  const wrap = el('div')
  if (!data) return el('p', 'empty', t('schedules.readFailed'))
  wrap.appendChild(el('div', `status-banner ${data.daemon_running ? 'on' : ''}`, data.daemon_running ? t('schedules.daemonOn') : t('schedules.daemonOff')))
  const list = el('div', 'schedule-list')
  for (const schedule of data.schedules || []) list.appendChild(buildScheduleCard(schedule))
  if (!list.children.length) list.appendChild(el('p', 'empty', t('tasks.noneScheduled')))
  wrap.appendChild(list)
  const button = el('button', 'task-action', t('schedules.viewRuns'))
  button.onclick = () => navigateView('tasks')
  wrap.appendChild(button)
  return wrap
}

// 审批 / 定时 / 持仓 render as destination pages (see PAGES below), not as
// artifact-pane tabs. The pane is for what the agent produces.

const openReport = (title, source, meta) =>
  pane.open({
    key: `md:${title}`,
    title,
    glyph: '▤',
    build: () => window.WyckoffMd.renderMarkdown(source, meta)
  })

// ---- in-app browser --------------------------------------------------------

// The browser is a native view layered over the window, not a DOM node. This
// placeholder reserves the space and reports its geometry to the main process;
// an observer keeps them in sync as the pane resizes.
let browserBox = null
let browserObserver = null

function closeBrowser () {
  window.wyckoff.browser.hide()
  if (browserObserver) browserObserver.disconnect()
  browserObserver = null
  browserBox = null
}

function syncBrowserBounds () {
  if (!browserBox || !browserBox.isConnected) return
  const r = browserBox.getBoundingClientRect()
  if (r.width < 2 || r.height < 2) return
  window.wyckoff.browser.setBounds({
    x: Math.round(r.left),
    y: Math.round(r.top),
    width: Math.round(r.width),
    height: Math.round(r.height)
  })
}

const openBrowser = () =>
  pane.open({
    key: 'browser',
    title: t('tab.browser'),
    glyph: '◍',
    onHide: () => window.wyckoff.browser.hide(),
    onClose: closeBrowser,
    build: () => {
      const wrap = el('div', 'bwrap')
      const bar = el('div', 'bbar')
      const input = el('input', 'burl')
      input.type = 'text'
      input.placeholder = t('browser.placeholder')
      input.spellcheck = false
      const go = el('button', 'mbtn', t('action.open'))
      const navigate = async () => {
        const url = input.value.trim()
        if (!url) return
        const target = /^https?:\/\//i.test(url) ? url : `https://${url}`
        input.value = target
        const res = await window.wyckoff.browser.run('navigate', { url: target })
        if (!res.ok) sysLine(t('browser.openFailed', { error: res.error }), true)
      }
      go.onclick = navigate
      input.onkeydown = (e) => { if (e.key === 'Enter') navigate() }
      const back = el('button', 'mbtn', t('action.back'))
      back.onclick = () => window.wyckoff.browser.run('back', {})
      bar.appendChild(back)
      bar.appendChild(input)
      bar.appendChild(go)
      wrap.appendChild(bar)

      browserBox = el('div', 'bview')
      wrap.appendChild(browserBox)

      // Geometry is only known after layout; defer one frame.
      requestAnimationFrame(() => {
        syncBrowserBounds()
        window.wyckoff.browser.show({
          x: Math.round(browserBox.getBoundingClientRect().left),
          y: Math.round(browserBox.getBoundingClientRect().top),
          width: Math.round(browserBox.getBoundingClientRect().width),
          height: Math.round(browserBox.getBoundingClientRect().height)
        })
      })
      if (browserObserver) browserObserver.disconnect()
      // ResizeObserver catches size changes, but a sidebar toggle only shifts
      // the pane's x position, which it does not fire for.
      browserObserver = new ResizeObserver(syncBrowserBounds)
      browserObserver.observe(browserBox)
      return wrap
    }
  })

// Charts are per-symbol, so the tab key carries the symbol — opening a second
// stock adds a tab instead of replacing the first.
const liveCharts = new Map()

const disposeChart = (symbol) => {
  const chart = liveCharts.get(symbol)
  if (!chart) return
  chart.dispose()
  liveCharts.delete(symbol)
}

const openKline = (symbol) =>
  pane.open({
    key: `kline:${symbol}`,
    title: `${symbol} ${t('kline.title')}`,
    glyph: '◫',
    // NOTE: no onHide here — tabs.js fires it on tab *switch* too, and
    // disposing then would force a refetch every time the user switches back.
    // The prior chart is disposed inside build() instead.
    onClose: () => disposeChart(symbol),
    build: async () => {
      disposeChart(symbol)
      const wrap = el('div', 'klwrap')
      wrap.appendChild(el('p', 'empty', t('kline.loading')))
      // Bars and annotations share one fetched frame, so price and structure
      // cannot drift across two upstream snapshots or consume quota twice.
      // callWithError 而不是 collect：用户现在能手输代码，「认不出这个代码」
      // 和「这只票没有行情」得说清楚，一律显示「加载失败」等于把线索丢了。
      let data = null
      let failure = ''
      try {
        data = await callWithError('chart_data', { symbol, days: 320 })
      } catch (err) {
        failure = (err && err.message) || ''
      }
      if (!data || !data.bars) {
        wrap.replaceChildren(el('p', 'empty', failure || t('kline.failed')))
        return wrap
      }
      wrap.replaceChildren(klineHeader(symbol, data.bars))
      const chart = window.WyckoffKline.createKlineChart({ bars: data.bars })
      chart.addPainter(window.WyckoffAnnotations.createAnnotationPainter(data))
      wrap.appendChild(chart.node)
      liveCharts.set(symbol, chart)
      // Canvas colours are resolved once, so a theme switch needs a repaint.
      requestAnimationFrame(() => chart.resize())
      return wrap
    }
  })

// charts.js 的持仓行需要它，但 charts.js 先于 app.js 加载，所以挂到 window
// 而不是反向 import。
window.WyckoffOpenKline = (symbol) => openKline(symbol)

/**
 * 重开本轮画过标注的 K 线 tab，让新标注显示出来。
 *
 * 标注是在 tab 首次构建之后才落盘的，所以需要再取一次。只刷新本轮真正画过的
 * 图 —— 每轮无条件刷新会把 320 根 K 线重新拉一遍。
 */
function refreshDrawnCharts (symbols) {
  if (!symbols || !symbols.size) return
  for (const symbol of symbols) {
    if (pane.has(`kline:${symbol}`)) openKline(symbol)
  }
}

function klineHeader (symbol, bars) {
  const bar = el('div', 'klbar')
  bar.appendChild(el('span', 'klsym', symbol))
  bar.appendChild(el('span', 'klmeta', t('kline.bars', { count: (bars.close || []).length })))
  const legend = el('div', 'kllegend')
  const item = (color, label) => {
    const span = el('span', null)
    const dot = el('i')
    dot.style.background = color
    span.append(dot, document.createTextNode(label))
    return span
  }
  const cs = getComputedStyle(document.documentElement)
  const clay = (cs.getPropertyValue('--clay') || '').trim() || '#d97757'
  const up = (cs.getPropertyValue('--up') || '').trim() || '#e5484d'
  legend.append(item(clay, t('kline.legendRange')), item(up, t('kline.legendEvent')))
  legend.appendChild(el('span', null, t('kline.hint')))
  bar.appendChild(legend)
  return bar
}

// ---- appearance ------------------------------------------------------------

const root = document.documentElement
const osDark = window.matchMedia('(prefers-color-scheme: dark)')
// Declared here because applyAppearance() writes it and the keydown handler
// reads it; both run before the settings modal is ever opened.
let sendOnEnter = true

/** Apply appearance settings to <html>. Safe to call with partial data. */
function applyAppearance (cfg) {
  const c = cfg || {}
  const mode = c.desktop_appearance || 'system'
  const dark = mode === 'dark' || (mode === 'system' && osDark.matches)
  root.classList.toggle('dark', dark)
  root.classList.toggle('serif', (c.desktop_font_family || 'sans') === 'serif')
  root.classList.toggle('compact', (c.desktop_density || 'cozy') === 'compact')
  root.classList.toggle('no-motion', Boolean(c.desktop_reduce_motion))
  const scale = Number(c.desktop_font_scale) || 100
  root.style.setProperty('--fs', `${(13.5 * scale) / 100}px`)
  sendOnEnter = c.desktop_send_on_enter !== false
}

// Following the OS means reacting to it changing while the app is open.
osDark.addEventListener('change', () => {
  if (!setData || (setData.desktop_appearance || 'system') === 'system') applyAppearance(setData)
})

/** One settings section. Sections render on demand from cached data. */
function buildSection (sec, data) {
  const wrap = el('div')
  if (!data) {
    wrap.appendChild(el('p', 'empty', t('daemon.readSettingsFailed')))
    return wrap
  }
  if (sec === 'appearance') return buildAppearance(wrap, data)
  if (sec === 'behavior') return buildBehavior(wrap, data)
  if (sec === 'tone') return buildTone(wrap, data)
  if (sec === 'models') return buildModelsTab(wrap, data)
  if (sec === 'daemon') return buildDaemon(wrap)
  return buildAccountSec(wrap)
}

/** Scheduling status. The daemon's lifetime follows this app by design. */
function buildDaemon (wrap) {
  // 导航已写「定时任务」，不重复。
  wrap.appendChild(el('p', 'dlg-sub', t('schedules.daemonNote')))
  const host = el('div')
  host.appendChild(el('p', 'empty', t('common.loading')))
  wrap.appendChild(host)

  const render = async () => {
    const st = await collect('daemon_status').catch(() => null)
    host.replaceChildren()
    if (!st) {
      host.appendChild(el('p', 'empty', t('schedules.statusReadFailed')))
      return
    }

    const live = el('div', 'srow')
    const left = el('div', 'sleft')
    left.appendChild(el('span', 'slab', t('daemon.schedProcess')))
    left.appendChild(el('span', 'shint', st.running ? t('daemon.running') : t('daemon.notRunning')))
    live.appendChild(left)
    live.appendChild(el('span', st.running ? 'ok' : 'miss', st.running ? t('daemon.stateRunning') : t('daemon.stateStopped')))
    host.appendChild(live)

    // Surfaced only when present: a leftover launchd service would run tasks
    // even with the app closed, contradicting the intended model.
    if (st.installed) {
      const warn = el('div', 'srow')
      const wl = el('div', 'sleft')
      wl.appendChild(el('span', 'slab', t('daemon.autostart')))
      wl.appendChild(el('span', 'shint', t('daemon.autostartDetected')))
      warn.appendChild(wl)
      host.appendChild(warn)

      const btn = el('button', 'wel-c', t('daemon.removeAutostart'))
      btn.onclick = async () => {
        if (!window.confirm(t('daemon.removeConfirm'))) return
        btn.disabled = true
        btn.textContent = t('daemon.processing')
        const res = await collect('daemon_uninstall').catch(() => null)
        if (!res) sysLine(t('daemon.removeFailed'), true)
        await render()
      }
      host.appendChild(btn)
    }

    const refresh = el('button', 'wel-c', t('daemon.refresh'))
    refresh.onclick = () => render()
    host.appendChild(refresh)
  }

  render()
  return wrap
}

/** Persist one key, echo the outcome, and re-apply appearance immediately. */
async function saveKey (row, key, value) {
  if (setData) setData[key] = value
  applyAppearance(setData)
  const res = await collect('settings_set', { key, value })
  note(row, res ? t('common.saved') : t('common.saveFailed'), !res)
  return Boolean(res)
}

/** Segmented control: a small set of mutually exclusive choices. */
function segRow (label, key, current, choices, sub) {
  const row = el('div', 'srow')
  const left = el('div', 'sleft')
  left.appendChild(el('span', 'slab', label))
  if (sub) left.appendChild(el('span', 'shint', sub))
  row.appendChild(left)
  const seg = el('div', 'seg')
  for (const [value, text] of choices) {
    const btn = el('button', value === current ? 'seg-b on' : 'seg-b', text)
    btn.onclick = async () => {
      for (const other of seg.querySelectorAll('.seg-b')) other.classList.remove('on')
      btn.classList.add('on')
      await saveKey(row, key, value)
    }
    seg.appendChild(btn)
  }
  row.appendChild(seg)
  return row
}

/** Language row: a segmented control over the supported locales. */
function langRow () {
  const choices = i18n.available().map((code) => [code, t(`lang.${code}`)])
  const row = segRow(t('appearance.language'), '__lang__', i18n.getLang(), choices, t('appearance.languageHint'))
  // segRow wires each button to saveKey; rebind to i18n instead. The buttons
  // are the only .seg-b children of this row.
  const buttons = [...row.querySelectorAll('.seg-b')]
  buttons.forEach((btn, index) => {
    const code = choices[index][0]
    btn.onclick = () => {
      for (const other of buttons) other.classList.remove('on')
      btn.classList.add('on')
      // Switching re-renders the whole settings body (and the app) in the new
      // language via the i18n change listener registered below.
      i18n.setLang(code)
    }
  })
  return row
}

function buildAppearance (wrap, data) {
  // 不再重复一遍「外观」：左边导航已经写着它了，页头再写一次没有信息量。
  // 面板里的标题留给真正的内容分类。
  wrap.appendChild(el('h3', 'sec', t('appearance.groupLanguage')))

  // Language is a client-only preference (localStorage), so it saves through
  // i18n rather than the server round-trip the other rows use.
  wrap.appendChild(langRow())

  wrap.appendChild(el('h3', 'sec', t('appearance.groupDisplay')))
  wrap.appendChild(segRow(t('appearance.theme'), 'desktop_appearance', data.desktop_appearance,
    [['system', t('appearance.themeSystem')], ['light', t('appearance.themeLight')], ['dark', t('appearance.themeDark')]],
    t('appearance.themeHint')))
  wrap.appendChild(segRow(t('appearance.font'), 'desktop_font_family', data.desktop_font_family,
    [['sans', t('appearance.fontSans')], ['serif', t('appearance.fontSerif')]]))
  wrap.appendChild(segRow(t('appearance.density'), 'desktop_density', data.desktop_density,
    [['cozy', t('appearance.densityCozy')], ['compact', t('appearance.densityCompact')]]))

  // A slider gives live feedback; the value is clamped server-side too.
  const row = el('div', 'srow')
  const left = el('div', 'sleft')
  left.appendChild(el('span', 'slab', t('appearance.fontScale')))
  const pct = el('span', 'shint', `${data.desktop_font_scale}%`)
  left.appendChild(pct)
  row.appendChild(left)
  const rng = el('input', 'rng')
  rng.type = 'range'
  rng.min = '80'
  rng.max = '140'
  rng.step = '5'
  rng.value = String(data.desktop_font_scale)
  rng.oninput = () => {
    pct.textContent = `${rng.value}%`
    // Preview while dragging without writing to disk on every step.
    root.style.setProperty('--fs', `${(13.5 * Number(rng.value)) / 100}px`)
  }
  rng.onchange = () => saveKey(row, 'desktop_font_scale', Number(rng.value))
  row.appendChild(rng)
  wrap.appendChild(row)
  return wrap
}

function buildBehavior (wrap, data) {
  wrap.appendChild(el('h3', 'sec', t('behavior.groupInput')))
  wrap.appendChild(segRow(t('behavior.sendMode'), 'desktop_send_on_enter', data.desktop_send_on_enter,
    [[true, t('behavior.sendEnter')], [false, t('behavior.sendCmdEnter')]],
    t('behavior.sendHint')))

  wrap.appendChild(el('h3', 'sec', t('behavior.groupMotion')))
  wrap.appendChild(segRow(t('behavior.motion'), 'desktop_reduce_motion', data.desktop_reduce_motion,
    [[false, t('behavior.motionNormal')], [true, t('behavior.motionReduced')]]))
  return wrap
}

// tone id -> i18n key stems; label/desc resolved at render so a language switch
// re-reads them rather than freezing the load-time language.
const TONES = [
  ['default', 'tone.default', 'tone.defaultDesc'],
  ['brief', 'tone.brief', 'tone.briefDesc'],
  ['detailed', 'tone.detailed', 'tone.detailedDesc'],
  ['evidence', 'tone.evidence', 'tone.evidenceDesc'],
  ['custom', 'tone.custom', 'tone.customDesc']
]

function buildTone (wrap, data) {
  // 只有一类内容，不需要分类标题 —— 加了就是白占一层。说明文字保留。
  wrap.appendChild(el('p', 'dlg-sub', t('tone.note')))

  const box = el('div', 'tone-l')
  const custom = el('div', 'tone-c')
  const ta = el('textarea', 'tone-t')
  ta.rows = 4
  ta.maxLength = 600
  ta.placeholder = t('tone.customPlaceholder')
  ta.value = data.desktop_tone_custom || ''

  const syncCustom = (tone) => { custom.hidden = tone !== 'custom' }

  for (const [value, labelKey, descKey] of TONES) {
    const opt = el('button', value === data.desktop_tone ? 'tone-o on' : 'tone-o')
    opt.appendChild(el('span', 'tone-n', t(labelKey)))
    opt.appendChild(el('span', 'tone-d', t(descKey)))
    opt.onclick = async () => {
      for (const other of box.querySelectorAll('.tone-o')) other.classList.remove('on')
      opt.classList.add('on')
      syncCustom(value)
      await saveKey(box, 'desktop_tone', value)
    }
    box.appendChild(opt)
  }
  wrap.appendChild(box)

  const save = el('button', 'wel-c', t('tone.saveCustom'))
  save.onclick = () => saveKey(custom, 'desktop_tone_custom', ta.value)
  custom.appendChild(ta)
  custom.appendChild(save)
  syncCustom(data.desktop_tone)
  wrap.appendChild(custom)
  return wrap
}

/** Models, data sources and timeouts — one tab, they are all "what it runs on". */
function buildModelsTab (wrap, data) {
  buildModels(wrap, data)

  wrap.appendChild(el('h3', 'sec', t('timeout.heading')))
  wrap.appendChild(el('p', 'dlg-sub', t('timeout.note')))
  wrap.appendChild(numRow(t('timeout.stream'), 'stream_chunk_timeout_seconds',
    data.stream_chunk_timeout_seconds, 10, 600))
  wrap.appendChild(numRow(t('timeout.tool'), 'tool_timeout_seconds',
    data.tool_timeout_seconds, 5, 300))

  wrap.appendChild(el('h3', 'sec', t('datasource.heading')))
  wrap.appendChild(el('p', 'dlg-sub', t('datasource.note')))
  wrap.appendChild(keyRow('TickFlow', data.has_tickflow_key))
  wrap.appendChild(keyRow('Tushare', data.has_tushare_token))
  return wrap
}

function buildAccountSec (wrap) {
  // 单一内容，无需分类标题。
  const row = el('div', 'srow')
  row.appendChild(el('span', 'slab', t('signin.current')))
  row.appendChild(el('span', signedIn ? 'ok' : 'miss', acctEmail || t('account.signedOut')))
  wrap.appendChild(row)
  if (signedIn) {
    wrap.appendChild(el('p', 'dlg-sub', t('signin.signoutNote')))
    const btn = el('button', 'wel-c', t('signin.signout'))
    btn.onclick = () => { closeSettings(); doSignOut() }
    wrap.appendChild(btn)
  } else {
    wrap.appendChild(el('p', 'dlg-sub', t('signin.reloginHint')))
  }
  return wrap
}

/**
 * Reload settings and repaint the whole section. Repainting via showSection
 * rather than a captured host element: role changes alter tags on OTHER rows
 * too, and a stale host reference silently no-ops after a section switch.
 */
async function refreshModels () {
  setData = await collect('settings_get').catch(() => null)
  if (!setData || setOv.hidden) return
  showSection('models')
}

function buildModels (wrap, data) {
  wrap.appendChild(el('h3', 'sec', t('models.heading')))
  wrap.appendChild(el('p', 'dlg-sub', t('models.note')))
  const host = el('div')
  wrap.appendChild(host)
  buildModelList(host, data)
  return wrap
}

function buildModelList (host, data) {
  // With 11 configured models an unfiltered list scrolls for three screens and
  // buries the only two rows that matter. Show the active pair, fold the rest.
  const active = data.models.filter((m) => m.id === data.default_model || m.id === data.fallback_model)
  const rest = data.models.filter((m) => m.id !== data.default_model && m.id !== data.fallback_model)

  for (const m of active) host.appendChild(modelRow(m, data))

  if (rest.length) {
    const more = el('div', 'mmore')
    const toggle = el('button', 'mtoggle', t('models.others', { count: rest.length }))
    const list = el('div')
    list.hidden = true
    toggle.onclick = () => {
      list.hidden = !list.hidden
      toggle.textContent = list.hidden ? t('models.others', { count: rest.length }) : t('models.othersCollapse')
      toggle.classList.toggle('open', !list.hidden)
    }
    for (const m of rest) list.appendChild(modelRow(m, data))
    more.appendChild(toggle)
    more.appendChild(list)
    host.appendChild(more)
  }

  host.appendChild(addModelForm())
}

/** One model: identity, role buttons, connectivity test, delete. */
function modelRow (m, data) {
  const row = el('div', 'mrow')
  const isDefault = m.id === data.default_model
  const isFallback = m.id === data.fallback_model

  const info = el('div', 'minfo')
  const title = el('div', 'mtitle')
  title.appendChild(el('span', 'mid', m.id))
  if (isDefault) title.appendChild(el('span', 'tag pri', t('models.tagDefault')))
  if (isFallback) title.appendChild(el('span', 'tag alt', t('models.tagFallback')))
  if (!m.has_key) title.appendChild(el('span', 'tag warn', t('models.tagNoKey')))
  info.appendChild(title)
  const sub = [m.provider_name, m.model, m.base_url].filter(Boolean).join(' · ')
  const subEl = el('span', 'msub', sub)
  // Ellipsised in CSS, so expose the full value on hover.
  subEl.title = sub
  info.appendChild(subEl)
  row.appendChild(info)

  const acts = el('div', 'macts')

  // Only the roles this model does NOT hold get a button. With 11 models,
  // showing all four actions per row put 44 buttons on one page — the two
  // that matter (which is primary, which is backup) drowned in noise.
  if (!isDefault) {
    const btn = el('button', 'mbtn', t('models.setDefault'))
    btn.onclick = async () => {
      const res = await collect('settings_set', { key: 'default_model', value: m.id })
      if (!res) { note(row, t('models.saveFailed'), true); return }
      await refreshModels()
    }
    acts.appendChild(btn)
  }
  if (!isFallback && !isDefault) {
    const btn = el('button', 'mbtn', t('models.setFallback'))
    btn.onclick = async () => {
      const res = await collect('settings_set', { key: 'fallback_model', value: m.id })
      if (!res) { note(row, t('models.saveFailed'), true); return }
      await refreshModels()
    }
    acts.appendChild(btn)
  }

  // Connectivity test: a real request, so the result is trustworthy.
  const test = el('button', 'mbtn', t('models.test'))
  test.onclick = async () => {
    test.disabled = true
    test.textContent = t('models.testing')
    const res = await collect('model_test', { id: m.id }).catch(() => null)
    test.disabled = false
    test.textContent = t('models.test')
    if (!res) { note(row, t('models.testFailed'), true); return }
    if (res.connected) note(row, t('models.connected', { ms: res.latency_ms }), false)
    else note(row, res.error || t('models.disconnected'), true)
  }
  acts.appendChild(test)

  // In-use models are deliberately not deletable: removing the model a running
  // analysis depends on fails mid-flight. Switch roles first.
  if (!isDefault && !isFallback) {
    const del = el('button', 'mbtn danger', t('models.delete'))
    del.onclick = async () => {
      if (!window.confirm(t('models.deleteConfirm', { id: m.id }))) return
      const res = await collect('model_remove', { id: m.id }).catch(() => null)
      if (!res) { note(row, t('models.deleteFailed'), true); return }
      await refreshModels()
    }
    acts.appendChild(del)
  }

  row.appendChild(acts)
  return row
}

/** Collapsed by default: adding a model is occasional, not the main task. */
function addModelForm () {
  const box = el('div', 'madd')
  const toggle = el('button', 'wel-c', t('models.addCustom'))
  const form = el('div', 'mform')
  form.hidden = true
  toggle.onclick = () => {
    form.hidden = !form.hidden
    toggle.textContent = form.hidden ? t('models.addCustom') : t('models.cancelAdd')
  }
  box.appendChild(toggle)

  const fields = {}
  const field = (key, label, placeholder) => {
    const f = el('div', 'mfield')
    f.appendChild(el('label', 'mflab', label))
    const inp = el('input', 'mfin')
    inp.placeholder = placeholder
    fields[key] = inp
    f.appendChild(inp)
    return f
  }

  const prov = el('div', 'mfield')
  prov.appendChild(el('label', 'mflab', 'Provider'))
  const sel = el('select', 'sel')
  for (const p of ['openai', 'gemini', 'claude']) {
    const opt = el('option', null, p)
    opt.value = p
    sel.appendChild(opt)
  }
  prov.appendChild(sel)

  form.appendChild(field('id', t('models.fieldId'), t('models.fieldIdPlaceholder')))
  form.appendChild(prov)
  form.appendChild(field('model', t('models.fieldModel'), t('models.fieldModelPlaceholder')))
  form.appendChild(field('api_key', 'API Key', 'sk-…'))
  form.appendChild(field('base_url', t('models.fieldBaseUrl'), t('models.fieldBaseUrlPlaceholder')))

  const submit = el('button', 'wel-c', t('models.saveTest'))
  submit.onclick = async () => {
    const payload = {
      id: fields.id.value.trim(),
      provider_name: sel.value,
      model: fields.model.value.trim(),
      api_key: fields.api_key.value.trim(),
      base_url: fields.base_url.value.trim()
    }
    if (!payload.id || !payload.model || !payload.api_key) {
      note(form, t('models.required'), true)
      return
    }
    submit.disabled = true
    submit.textContent = t('models.saving')
    const saved = await collect('model_add', payload).catch(() => null)
    if (!saved) {
      submit.disabled = false
      submit.textContent = t('models.saveTest')
      note(form, t('models.saveCheckFields'), true)
      return
    }
    // Test right after saving: a model that cannot connect is worse than none,
    // because it will silently fail mid-analysis.
    submit.textContent = t('models.testingConn')
    const res = await collect('model_test', { id: payload.id }).catch(() => null)
    submit.disabled = false
    submit.textContent = t('models.saveTest')
    if (res && res.connected) sysLine(t('models.addedConnected', { id: payload.id, ms: res.latency_ms }))
    else sysLine(t('models.savedButFailed', { id: payload.id, error: (res && res.error) || t('models.unknownError') }), true)
    await refreshModels()
  }
  form.appendChild(submit)
  box.appendChild(form)
  return box
}

function note (row, text, isError) {
  const old = row.querySelector('.snote')
  if (old) old.remove()
  const tag = el('span', isError ? 'snote err' : 'snote', text)
  row.appendChild(tag)
  setTimeout(() => tag.remove(), 2200)
}

function numRow (label, key, value, min, max) {
  const row = el('div', 'srow')
  row.appendChild(el('span', 'slab', label))
  const inp = el('input', 'num')
  inp.type = 'number'
  inp.value = String(value)
  inp.min = String(min)
  inp.max = String(max)
  inp.onchange = async () => {
    const n = Number(inp.value)
    // Clamp locally so the backend never sees an out-of-range value.
    if (!Number.isFinite(n) || n < min || n > max) {
      inp.value = String(value)
      note(row, t('common.range', { min, max }), true)
      return
    }
    const res = await collect('settings_set', { key, value: n })
    note(row, res ? t('common.saved') : t('common.saveFailed'), !res)
  }
  row.appendChild(inp)
  return row
}

function keyRow (label, present) {
  const row = el('div', 'srow')
  row.appendChild(el('span', 'slab', label))
  row.appendChild(el('span', present ? 'ok' : 'miss', present ? t('datasource.configured') : t('datasource.notConfigured')))
  return row
}

// ---- settings modal --------------------------------------------------------

const setOv = document.getElementById('set-ov')
const setBody = document.getElementById('set-body')
let setData = null

function showSection (sec) {
  for (const b of document.querySelectorAll('.dlg-n')) {
    b.classList.toggle('on', b.dataset.sec === sec)
  }
  setBody.replaceChildren(buildSection(sec, setData))
  setBody.scrollTop = 0
}

function closeSettings () {
  setOv.hidden = true
}

async function openSettings (sec) {
  setOv.hidden = false
  setBody.replaceChildren(el('p', 'empty', t('common.loading')))
  // Refetch each time: the CLI can change config behind the app's back.
  setData = await collect('settings_get').catch(() => null)
  if (setOv.hidden) return
  showSection(sec || 'appearance')
}

/** Load appearance before the user opens settings, so launch honours it. */
async function loadAppearance () {
  setData = await collect('settings_get').catch(() => null)
  applyAppearance(setData)
}

for (const btn of document.querySelectorAll('.dlg-n')) {
  btn.onclick = () => showSection(btn.dataset.sec)
}
document.getElementById('set-close').onclick = closeSettings
// Backdrop click closes; clicks inside the dialog must not bubble out to it.
setOv.onclick = (e) => { if (e.target === setOv) closeSettings() }

// Destination pages render into the main area. Previously every nav item threw
// a panel into the right-hand artifact pane, which turned that pane into a
// dumping ground and left the main area showing an unrelated conversation.
// title/sub are i18n keys, not literal text, so a language switch re-resolves
// them on the next render rather than freezing whatever language loaded first.
const PAGES = {
  tasks: {
    titleKey: 'tasks.heading',
    subKey: 'tasks.pageSub',
    wide: true,
    build: async () => {
      const [approvals, schedules] = await Promise.all([
        collect('approve_list'),
        collect('schedules')
      ])
      return buildTasks(approvals, schedules)
    }
  },
  approvals: {
    titleKey: 'nav.approvals',
    subKey: 'approvals.pageSub',
    build: async () => {
      const [approvals, account] = await Promise.all([
        collect('approve_list'),
        collect('account')
      ])
      return buildApprovals(approvals, account)
    }
  },
  schedules: {
    titleKey: 'schedules.heading',
    subKey: 'schedules.pageSub',
    build: async () => buildSchedules(await collect('schedules'))
  },
  portfolio: {
    titleKey: 'tab.charts',
    subKey: 'portfolio.pageSub',
    wide: true,
    build: async () => {
      const data = await collect('portfolio')
      return window.WyckoffCharts.renderCharts((data && data.portfolio) || {})
    }
  },
  reports: {
    titleKey: 'nav.reports',
    subKey: 'reports.pageSub',
    wide: true,
    build: buildReportPage
  }
}

const page = document.getElementById('page')
const pageBody = document.getElementById('page-body')
let pageToken = 0
let activePage = null
let pageCleanup = null

async function buildReportPage () {
  const viewer = window.createArtifactViewer({
    call: collect,
    onError: (message) => sysLine(message, true)
  })
  await viewer.refresh()
  const wrap = el('div', 'report-page')
  wrap.appendChild(viewer.node)
  return { node: wrap, dispose: viewer.dispose }
}

function clearPage () {
  if (pageCleanup) pageCleanup()
  pageCleanup = null
}

function showChat () {
  pageToken += 1
  clearPage()
  activePage = null
  page.hidden = true
  stream.hidden = false
}

async function showPage (name) {
  const spec = PAGES[name]
  if (!spec) return showChat()
  activePage = name
  stream.hidden = true
  page.hidden = false
  page.classList.toggle('wide', Boolean(spec.wide))
  document.getElementById('page-title').textContent = t(spec.titleKey)
  document.getElementById('page-sub').textContent = t(spec.subKey)
  clearPage()
  pageBody.replaceChildren(el('p', 'empty', t('tab.loading')))
  // Guard against a slower earlier page painting over a newer one.
  const token = ++pageToken
  const built = await spec.build()
  if (token !== pageToken) {
    if (built && built.dispose) built.dispose()
    return
  }
  const node = built && built.node ? built.node : built
  pageCleanup = built && built.dispose ? built.dispose : null
  pageBody.replaceChildren(node)
}

function selectNav (view) {
  for (const nav of document.querySelectorAll('.nv')) {
    nav.classList.toggle('on', nav.dataset.view === view)
  }
}

function navigateView (view) {
  selectNav(view)
  if (view === 'chat') showChat()
  else showPage(view)
}

for (const nav of document.querySelectorAll('.nv')) nav.onclick = () => navigateView(nav.dataset.view)
selectNav('chat')

document.getElementById('btn-new-analysis').onclick = () => {
  navigateView('chat')
  input.focus()
}

btnSend.onclick = send
btnRestart.onclick = () => window.wyckoff.restart()

// Reports, browser and the artifact pane are content the agent produces, not
// navigation. They live behind a labelled "打开" menu rather than a row of
// glyphs nobody could decode.
const openBtn = document.getElementById('btn-open')
const openMenu = document.getElementById('open-menu')

function setOpenMenu (open) {
  openMenu.hidden = !open
  openBtn.classList.toggle('on', open)
  openBtn.setAttribute('aria-expanded', open ? 'true' : 'false')
  if (!open) return
  // Anchor below the trigger, right-aligned, clamped to stay on screen.
  const r = openBtn.getBoundingClientRect()
  const left = Math.min(r.right - openMenu.offsetWidth, window.innerWidth - openMenu.offsetWidth - 8)
  openMenu.style.left = `${Math.max(8, left)}px`
  openMenu.style.top = `${r.bottom + 6}px`
}

openBtn.onclick = (e) => {
  e.stopPropagation()
  setOpenMenu(openMenu.hidden)
}

// ---- 手动打开 K 线图 -------------------------------------------------------
// 图表原本只有 agent 能开（annotate_chart 执行时顺带开一个 tab），所以用户想
// 单纯看某只票的图，得先设法让模型去标注它。这里补上直接的入口。

let symBox = null

function closeSymBox () {
  // 按 DOM 查而不是只看 symBox：浮层要到下一帧才登记，这中间也得能关掉。
  for (const node of document.querySelectorAll('.symbox')) node.remove()
  symBox = null
}

function openSymBox () {
  closeSymBox()
  const box = el('div', 'menu symbox')
  box.appendChild(el('div', 'symbox-label', t('chart.promptLabel')))
  const row = el('div', 'symbox-row')
  const input = el('input')
  input.type = 'text'
  input.placeholder = t('chart.placeholder')
  input.setAttribute('aria-label', t('chart.promptLabel'))
  const go = el('button', 'b pri', t('chart.open'))
  go.type = 'button'
  row.append(input, go)
  box.appendChild(row)
  const hint = el('p', 'symbox-hint', t('chart.hint'))
  box.appendChild(hint)

  const submit = () => {
    const raw = input.value.trim()
    if (!raw) return
    // 只挡明显不可能的输入，真正的代码规则由后端那份正规化决定 ——
    // 在这里复刻一套正则，两份规则迟早会不一致。
    if (!/^[A-Za-z0-9.]{1,12}$/.test(raw)) {
      hint.className = 'symbox-hint bad'
      hint.textContent = t('chart.badSymbol')
      return
    }
    closeSymBox()
    setPane(true)
    openKline(raw.toUpperCase())
  }
  go.onclick = submit
  input.onkeydown = (e) => {
    if (e.key === 'Enter') submit()
    if (e.key === 'Escape') closeSymBox()
  }
  // 点浮层内部不该把它关掉（下面挂了全局关闭）。
  box.onclick = (e) => e.stopPropagation()

  document.body.appendChild(box)
  const r = openBtn.getBoundingClientRect()
  const left = Math.min(r.right - box.offsetWidth, window.innerWidth - box.offsetWidth - 8)
  box.style.left = `${Math.max(8, left)}px`
  box.style.top = `${r.bottom + 6}px`
  symBox = box
  input.focus()
}

const menuAction = (fn) => () => { setOpenMenu(false); fn() }
document.getElementById('mi-chart').onclick = menuAction(openSymBox)
document.getElementById('mi-reports').onclick = menuAction(() => navigateView('reports'))
document.getElementById('mi-browser').onclick = menuAction(openBrowser)
document.getElementById('mi-pane').onclick = menuAction(() => {
  if (win.classList.contains('pane-on')) pane.closeAll()
  else setPane(true)
})
// The chip is a shortcut to the approvals page; keep the sidebar in sync so
// the highlighted nav item always matches what is on screen.
document.getElementById('pending-chip').onclick = () => {
  navigateView('approvals')
}

input.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter') return
  // Two modes: Enter sends (Shift+Enter for newline), or Cmd/Ctrl+Enter sends
  // and a bare Enter inserts a newline. These orders touch money, so the user
  // picks which one is harder to trigger by accident.
  const shouldSend = sendOnEnter ? !e.shiftKey && !e.metaKey && !e.ctrlKey : e.metaKey || e.ctrlKey
  if (!shouldSend) return
  e.preventDefault()
  send()
})
input.addEventListener('input', () => {
  input.style.height = 'auto'
  input.style.height = `${Math.min(input.scrollHeight, 160)}px`
})

/**
 * Welcome state. The composer sits inside it, centred, until the first message;
 * then the SAME node moves to the bottom host and the thread starts scrolling.
 */
function greeting () {
  const h = new Date().getHours()
  if (h < 6) return t('welcome.night')
  if (h < 12) return t('welcome.morning')
  if (h < 18) return t('welcome.afternoon')
  return t('welcome.evening')
}

/** Move the composer to the bottom and hide the welcome block. Idempotent. */
function enterChat () {
  if (chatting) return
  chatting = true
  document.getElementById('comp-host-bottom').appendChild(document.getElementById('comp'))
  thread.classList.add('chatting')
}

// [labelKey, promptKey]; resolved at render so the cards follow the language.
const PROMPTS = [
  ['prompt.todayName', 'prompt.today'],
  ['prompt.holdingsName', 'prompt.holdings'],
  ['prompt.stopsName', 'prompt.stops'],
  ['prompt.reviewName', 'prompt.review']
]

function buildWelcomeCards () {
  const grid = document.getElementById('wel-cards')
  grid.replaceChildren()
  for (const [labelKey, promptKey] of PROMPTS) {
    const prompt = t(promptKey)
    const btn = el('button', 'wel-c', t(labelKey))
    btn.title = prompt
    // Prefill rather than send: these touch money, so the user presses send.
    btn.onclick = () => {
      input.value = prompt
      input.focus()
      input.dispatchEvent(new Event('input'))
    }
    grid.appendChild(btn)
  }
}

function welcomeMetric (label, value, view) {
  const button = el('button', 'wel-metric')
  button.appendChild(el('b', 'tnum', String(value)))
  button.appendChild(el('span', null, label))
  button.onclick = () => navigateView(view)
  return button
}

function attentionAction (text, action) {
  const button = el('button', null, text)
  button.onclick = action
  return button
}

function buildTodayOverview ({ positions, pending, noStop, schedules }) {
  const enabled = (schedules.schedules || []).filter((item) => item.enabled).length
  const overview = document.getElementById('wel-overview')
  overview.replaceChildren(
    welcomeMetric(t('welcome.positionsMetric'), positions.length, 'portfolio'),
    welcomeMetric(t('welcome.approvalsMetric'), pending, 'approvals'),
    welcomeMetric(t('welcome.schedulesMetric'), enabled, 'tasks'),
    welcomeMetric(t('welcome.riskMetric'), noStop, 'portfolio')
  )

  const attention = document.getElementById('wel-attention')
  attention.replaceChildren(el('div', 'wel-attention-title', t('welcome.needsAttention')))
  if (pending) attention.appendChild(attentionAction(t('welcome.approvalAttention', { count: pending }), () => navigateView('approvals')))
  if (noStop) {
    attention.appendChild(attentionAction(t('welcome.stopAttention', { count: noStop }), () => {
      input.value = t('prompt.stops')
      input.focus()
      input.dispatchEvent(new Event('input'))
    }))
  }
  if (enabled && !schedules.daemon_running) {
    attention.appendChild(attentionAction(t('welcome.schedulerAttention'), () => navigateView('schedules')))
  }
  attention.hidden = attention.children.length === 1
}

/** Summarise real state; never invent numbers when a call fails. */
async function loadWelcome () {
  document.getElementById('wel-greet').textContent = greeting()
  buildWelcomeCards()

  const [approvals, pf, schedules] = await Promise.all([
    collect('approve_list').catch(() => null),
    collect('portfolio').catch(() => null),
    collect('schedules').catch(() => null)
  ])
  // The user may have started chatting while these were in flight.
  if (chatting) return

  const parts = []
  const pending = approvals && approvals.count ? approvals.count : 0
  const positions = (pf && pf.portfolio && pf.portfolio.positions) || []
  const noStop = positions.filter((p) => p.stop_loss === null || p.stop_loss === undefined).length

  parts.push(positions.length ? t('welcome.holding', { count: positions.length }) : t('welcome.noHolding'))
  if (noStop) parts.push(t('welcome.noStop', { count: noStop }))
  parts.push(pending ? t('welcome.pending', { count: pending }) : t('welcome.noPending'))

  document.getElementById('wel-sum').textContent = parts.join(' · ')
  buildTodayOverview({ positions, pending, noStop, schedules: schedules || { schedules: [] } })
}

// ---- account menu ----------------------------------------------------------

const acctBtn = document.getElementById('btn-acct')
const acctMenu = document.getElementById('acct-menu')
let signedIn = false
let acctEmail = ''

function setMenu (open) {
  acctMenu.hidden = !open
  acctBtn.setAttribute('aria-expanded', open ? 'true' : 'false')
  if (!open) return
  // Anchor above the row; clamp so a short window cannot push it off-screen.
  const r = acctBtn.getBoundingClientRect()
  acctMenu.style.left = `${Math.max(8, r.left + 6)}px`
  acctMenu.style.top = `${Math.max(8, r.top - acctMenu.offsetHeight - 6)}px`
}

async function loadAccount () {
  const data = await collect('account').catch(() => null)
  signedIn = Boolean(data && data.signed_in)
  const email = (data && data.email) || ''
  acctEmail = email
  const label = signedIn ? (email || t('account.signedIn')) : t('account.signedOut')
  const initial = email ? email[0] : '·'

  document.getElementById('acct-name').textContent = label
  document.getElementById('acct-ava').textContent = initial
  document.getElementById('menu-email').textContent = label
  document.getElementById('menu-ava').textContent = initial
  // Signing out is meaningless when there is no session.
  document.getElementById('mi-signout').hidden = !signedIn
}

acctBtn.onclick = (e) => {
  e.stopPropagation()
  setMenu(acctMenu.hidden)
}

document.getElementById('mi-settings').onclick = () => {
  setMenu(false)
  openSettings()
}

/** Destructive: clears the stored session AND credentials, so no auto-relogin. */
async function doSignOut () {
  if (!window.confirm(t('signin.signoutConfirm'))) return
  const res = await collect('sign_out').catch(() => null)
  if (!res) {
    sysLine(t('signin.signoutFailed'), true)
    return
  }
  await loadAccount()
  sysLine(t('signin.signedOutDone'))
}

document.getElementById('mi-signout').onclick = () => {
  setMenu(false)
  doSignOut()
}

// Outside click and Esc both dismiss, matching platform menu behaviour.
window.addEventListener('click', () => { if (!acctMenu.hidden) setMenu(false) })
window.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return
  // Innermost layer first: the modal sits above the menus.
  if (!setOv.hidden) closeSettings()
  else if (document.querySelector('.symbox')) closeSymBox()
  else if (!acctMenu.hidden) setMenu(false)
  else if (!openMenu.hidden) setOpenMenu(false)
})

document.getElementById('btn-side').onclick = () =>
  setSide(win.classList.contains('side-off'))

const togglePane = () => {
  // An empty pane holding stale tab headers is worse than no pane, so toggling
  // off closes its tabs. Reopening comes from content.
  if (win.classList.contains('pane-on')) pane.closeAll()
  else setPane(true)
}

// Dismiss the open menu on any outside click, like the account menu.
window.addEventListener('click', () => { if (!openMenu.hidden) setOpenMenu(false) })
// 点外面关掉浮层。要排除浮层自身和打开它的那个菜单项：打开它的那次 click
// 仍在往 window 冒泡（菜单本身靠这次冒泡关闭），否则浮层会开了就立刻被关。
window.addEventListener('click', (e) => {
  if (e.target.closest && e.target.closest('.symbox, #mi-chart')) return
  closeSymBox()
})

// Cmd/Ctrl shortcuts: B sidebar, ⌥B pane, R reports, T browser, K chart, , settings.
window.addEventListener('keydown', (e) => {
  if (!(e.metaKey || e.ctrlKey)) return
  const key = e.key.toLowerCase()
  if (e.key === ',') {
    e.preventDefault()
    if (setOv.hidden) openSettings()
    else closeSettings()
  } else if (key === 'b') {
    e.preventDefault()
    if (e.altKey) togglePane()
    else setSide(win.classList.contains('side-off'))
  } else if (key === 'r' && !e.altKey) {
    e.preventDefault()
    navigateView('reports')
  } else if (key === 't' && !e.altKey) {
    e.preventDefault()
    openBrowser()
  } else if (key === 'k' && !e.altKey) {
    e.preventDefault()
    openSymBox()
  }
})

// Paint the static HTML in the resolved language before anything else shows.
i18n.applyDom()

// A language switch re-renders everything dynamic: static nodes are handled by
// applyDom inside i18n.setLang; here we repaint the JS-built surfaces.
i18n.onChange(() => {
  // The header chip embeds a translated word ("待批"/"pending"); repaint it.
  refreshApprovals()
  // Welcome greeting + cards, unless the user has already started chatting.
  if (!chatting) {
    document.getElementById('wel-greet').textContent = greeting()
    buildWelcomeCards()
  }
  // The open settings section (labels, options, the language row itself).
  if (!setOv.hidden) {
    const active = document.querySelector('.dlg-n.on')
    showSection(active ? active.dataset.sec : 'appearance')
  }
  // The active destination page (title, subtitle, body).
  if (activePage) showPage(activePage)
})

window.wyckoff.onEvent(renderEvent)
window.wyckoff.onStatus(setStatus)
setBusy(false)

// Python may have reached ready before this listener existed; pull the current
// status so the UI does not sit on "连接中…" with no calls ever issued.
window.wyckoff.status().then(setStatus).catch(() => {})
