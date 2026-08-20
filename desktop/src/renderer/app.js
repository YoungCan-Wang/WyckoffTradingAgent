'use strict'

// i18n shorthand. resolveInitial picks user choice > system language > zh, and
// applyDom must run before first paint so the static HTML is not briefly zh.
const i18n = window.WyckoffI18n
const t = (key, params) => i18n.t(key, params)
i18n.setLang(i18n.resolveInitial(), { persist: false })

const stream = document.getElementById('stream')
const btnRestart = document.getElementById('btn-restart')
const paneBody = document.getElementById('pane-body')
const win = document.querySelector('.win')
const sideButton = document.getElementById('btn-side')
const sideSlot = document.getElementById('side-toggle-slot')
const threadSideSlot = document.getElementById('thread-toggle-slot')
const viewTitle = document.getElementById('view-title')
const paneResizer = document.getElementById('pane-resizer')
let browserBox = null
let browserObserver = null

let ready = false

// Panels start closed so the conversation owns the window, as in ChatGPT and
// Claude desktop. Sidebar choice persists; the artifact pane does not — it is
// opened by content, and a stale empty pane on launch is just dead space.
const SIDE_KEY = 'wyckoff.sidebar'
const PANE_WIDTH_KEY = 'wyckoff.pane.width'
const MIN_PANE_WIDTH = 360
const MIN_THREAD_WIDTH = 420

// A sidebar or window change shifts the pane horizontally without resizing the
// placeholder, so ResizeObserver never fires. Re-sync explicitly.
window.addEventListener('resize', () => {
  const paneWidth = document.getElementById('pane').getBoundingClientRect().width
  if (paneWidth) setPaneWidth(paneWidth, false)
  syncBrowserBounds()
})

function setSide (on) {
  win.classList.toggle('side-off', !on)
  // Codex uses one sidebar control and relocates it with the panel: inside the
  // sidebar while open, back in the thread toolbar while closed. Keeping one
  // real button also avoids duplicate tab stops and conflicting active states.
  const targetSlot = on ? sideSlot : threadSideSlot
  targetSlot.appendChild(sideButton)
  const titleKey = on ? 'tooltip.sidebarHide' : 'tooltip.sidebarShow'
  sideButton.dataset.i18nTitle = titleKey
  sideButton.title = t(titleKey)
  try { localStorage.setItem(SIDE_KEY, on ? '1' : '0') } catch { /* private mode */ }
  const paneWidth = document.getElementById('pane').getBoundingClientRect().width
  if (paneWidth) setPaneWidth(paneWidth, false)
  // The sidebar animates; re-sync after the transition settles.
  setTimeout(syncBrowserBounds, 220)
}

function setPane (on) {
  win.classList.toggle('pane-on', Boolean(on))
  // The browser is a native view floating above the DOM; hiding the pane must
  // detach it or it stays visible over the conversation.
  if (!on) window.wyckoff.browser.hide()
  if (on) requestAnimationFrame(syncBrowserBounds)
}

function paneWidthLimit () {
  const sideWidth = win.classList.contains('side-off') ? 0 : document.getElementById('side').offsetWidth
  return Math.max(MIN_PANE_WIDTH, win.clientWidth - sideWidth - MIN_THREAD_WIDTH)
}

function setPaneWidth (width, persist = true) {
  const next = Math.round(Math.min(Math.max(width, MIN_PANE_WIDTH), paneWidthLimit()))
  win.style.setProperty('--pane-width', `${next}px`)
  paneResizer.setAttribute('aria-valuenow', String(next))
  paneResizer.setAttribute('aria-valuemax', String(paneWidthLimit()))
  if (persist) {
    try { localStorage.setItem(PANE_WIDTH_KEY, String(next)) } catch { /* private mode */ }
  }
  syncBrowserBounds()
}

function restorePaneWidth () {
  let saved = 0
  try { saved = Number(localStorage.getItem(PANE_WIDTH_KEY)) } catch { /* private mode */ }
  setPaneWidth(saved >= MIN_PANE_WIDTH ? saved : Math.round(window.innerWidth * 0.46), false)
}

paneResizer.addEventListener('pointerdown', (event) => {
  event.preventDefault()
  paneResizer.setPointerCapture(event.pointerId)
  paneResizer.classList.add('dragging')
})
paneResizer.addEventListener('pointermove', (event) => {
  if (!paneResizer.hasPointerCapture(event.pointerId)) return
  setPaneWidth(window.innerWidth - event.clientX, false)
})
paneResizer.addEventListener('pointerup', (event) => {
  if (!paneResizer.hasPointerCapture(event.pointerId)) return
  paneResizer.releasePointerCapture(event.pointerId)
  paneResizer.classList.remove('dragging')
  const width = document.getElementById('pane').getBoundingClientRect().width
  setPaneWidth(width, true)
})
paneResizer.addEventListener('keydown', (event) => {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  const current = document.getElementById('pane').getBoundingClientRect().width
  if (event.key === 'Home') setPaneWidth(MIN_PANE_WIDTH)
  else if (event.key === 'End') setPaneWidth(paneWidthLimit())
  else setPaneWidth(current + (event.key === 'ArrowLeft' ? 24 : -24))
})

setSide((() => {
  try {
    const saved = localStorage.getItem(SIDE_KEY)
    if (saved !== null) return saved === '1'
  } catch { /* private mode */ }
  // A regular desktop window has room for navigation; compact windows start
  // content-first. Once chosen, the user's preference wins on later launches.
  return window.innerWidth >= 1180
})())
setPane(false)
restorePaneWidth()

const el = (tag, cls, text) => {
  const node = document.createElement(tag)
  if (cls) node.className = cls
  if (text !== undefined) node.textContent = text
  return node
}


// Messages go into .inner, which is width-capped and centred; #stream itself
// is the scroll container. Appending straight to #stream would bypass that cap.
const thread = document.querySelector('.thread')


/**
 * 系统提示行 —— 转发给 React 的对话流。
 *
 * app.js 不再拥有对话渲染，但退出登录、浏览器打开失败这类动作仍在这里，
 * 它们的提示要出现在同一条流里。React 尚未挂载时退回 console：宁可只进日志，
 * 也不要抛异常打断动作本身。
 */
function sysLine (text, isError) {
  const chat = window.WyckoffChat
  if (chat && chat.sysLine) chat.sysLine(text, Boolean(isError))
  else console[isError ? 'error' : 'log'](text)
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
  document.getElementById('stream').hidden = true
  box.hidden = false
}

function clearBackendError () {
  const box = document.getElementById('backend-error')
  if (box) box.hidden = true
  document.getElementById('stream').hidden = false
}

function setStatus (payload) {
  if (payload.state === 'log') return

  const wasReady = ready
  ready = payload.state === 'ready'

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
  } else if (payload.state === 'error') {
    // A calm empty state with a retry action — never raw diagnostics. The
    // technical detail already went to the logs from the main process.
    showBackendError(payload.reason)
  }
  // starting / restarting / stopped stay silent: the header status dot already
  // reflects them, and auto-restart is in flight.
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

/**
 * 侧栏徽章。两个徽章的口径必须和它们各自那一页的口径一致 ——
 * 徽章是那页内容的摘要，不是另一套数字。
 *
 * 之前两个徽章都显示「定时任务总数」，于是两处对不上：
 * - 首页「启用计划」只数 enabled 的，两个任务都未启用时显示 0，
 *   而侧栏徽章显示 2，看着像有两个在跑。
 * - 「任务运行」页数的是 enabled + 待审批，与总数无关。
 */
async function refreshSchedules () {
  const data = await collect('schedules')
  if (!data) return
  const schedules = data.schedules || []
  const enabled = schedules.filter((item) => item.enabled).length
  // 只有启用的才算「在跑」；未启用的任务不该催用户去看。
  document.getElementById('schedule-count').textContent = enabled ? String(enabled) : ''

  // 「任务运行」页把待审批也算进需要处理的量，徽章跟着同一口径。
  const approvals = await collect('approve_list').catch(() => null)
  const pending = (approvals && approvals.count) || 0
  const attention = enabled + pending
  const badge = document.getElementById('task-count')
  badge.textContent = attention ? String(attention) : ''
  // 有待审批时标红：那是真的在等人，跟「有几个计划开着」不同量级。
  badge.classList.toggle('warn', pending > 0)
}

// The pane reports how many tabs it holds, so visibility follows content:
// it appears when something opens and disappears when the last tab closes.
// Every open path gets this for free instead of toggling at each call site.
const pane = new window.WyckoffTabs.TabPane('tabs', 'pane-body', {
  onCountChange: (count) => {
    const hasArtifacts = count > 0
    document.getElementById('mi-pane').hidden = !hasArtifacts
    document.getElementById('pane-menu-sep').hidden = !hasArtifacts
    if (hasArtifacts) setPane(true)
    else setPane(false)
  }
})

const openReport = (title, source, meta) =>
  pane.open({
    key: `md:${title}`,
    title,
    icon: 'file-text',
    build: () => window.WyckoffMd.renderMarkdown(source, meta)
  })

// ---- in-app browser --------------------------------------------------------

// The browser is a native view layered over the window, not a DOM node. This
// placeholder reserves the space and reports its geometry to the main process;
// an observer keeps them in sync as the pane resizes.
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
    icon: 'globe-2',
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
    icon: 'chart-candlestick',
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




/**
 * Reload settings and repaint the whole section. Repainting via showSection
 * rather than a captured host element: role changes alter tags on OTHER rows
 * too, and a stale host reference silently no-ops after a section switch.
 */
/**
 * 重读配置并刷新输入框的模型选择器。
 *
 * 设置面板不用管：那边由 React 自己 reload 重渲染。以前这里会强制
 * showSection('agent')，现在那样做只会白重挂一次 React 并丢掉滚动位置。
 */
async function refreshModels () {
  setData = await collect('settings_get').catch(() => null)
  if (!setData) return
  window.dispatchEvent(new Event('wyckoff:models-changed'))
}


// ---- settings modal --------------------------------------------------------

const setOv = document.getElementById('set-ov')
const setBody = document.getElementById('set-body')
let setData = null
let settingsOpener = null

const focusableWithin = (root) => [...root.querySelectorAll(
  'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [href], [tabindex]:not([tabindex="-1"])'
)].filter((node) => !node.hidden && node.getClientRects().length > 0)

/**
 * 显示某个设置页。anchor 是可选的分栏标题 i18n key —— 给了就把那一栏滚到
 * 顶部，而不是停在页面开头。
 *
 * 用 key 而不是下标或文本：下标会随分栏增减而错位，文本在英文界面下对不上。
 */
function showSection (sec, anchor) {
  for (const b of document.querySelectorAll('.dlg-n')) {
    const selected = b.dataset.sec === sec
    b.classList.toggle('on', selected)
    b.setAttribute('aria-current', selected ? 'page' : 'false')
  }
  // 三页全部由 React 渲染，不再需要分派分支。
  window.WyckoffReact.mountSettings(setBody, sec)
  // 无锚点时立刻回到顶部；有锚点则等标题出现（见 scrollToAnchor 的重试）。
  scrollToAnchor(anchor)
}

/**
 * 把指定分栏滚到内容区顶部。找不到就回到开头 —— 宁可停在顶部，
 * 也不要因为 key 写错而停在一个看不出所以然的位置。
 */
function scrollToAnchor (anchor, attempt = 0) {
  if (!anchor) {
    setBody.scrollTop = 0
    return
  }
  const head = setBody.querySelector(`.sec[data-sec-key="${anchor}"]`)
  if (!head) {
    // React 渲染是异步的，而且分栏标题要等组件内部读完设置才出现，
    // 所以一帧不够。重试有限次数，超时就老实回到顶部。
    if (attempt < 20) {
      requestAnimationFrame(() => scrollToAnchor(anchor, attempt + 1))
      return
    }
    setBody.scrollTop = 0
    return
  }
  // offsetTop 是相对定位父级的，这里用两者的 rect 差值，不依赖布局层级。
  const delta = head.getBoundingClientRect().top - setBody.getBoundingClientRect().top
  // 减掉标题自身的上边距，让它真正贴顶而不是半截露在外面。
  const pad = parseFloat(getComputedStyle(head).marginTop) || 0
  setBody.scrollTop += delta - pad
}

function closeSettings () {
  if (setOv.hidden) return
  setOv.hidden = true
  win.inert = false
  const target = settingsOpener
  settingsOpener = null
  if (target && target.isConnected && !target.closest('[hidden]')) target.focus()
}

async function openSettings (sec, anchor) {
  if (setOv.hidden) {
    const active = document.activeElement
    if (active && document.getElementById('acct-menu').contains(active)) settingsOpener = document.getElementById('btn-acct')
    else if (active && document.getElementById('open-menu').contains(active)) settingsOpener = document.getElementById('btn-open')
    else settingsOpener = active
  }
  setOv.hidden = false
  win.inert = true
  // 不要在这里 replaceChildren：#set-body 由 React root 拥有，从外面摘掉它的
  // 节点会让下一次 render 报 NotFoundError。加载态由 React 自己显示。
  showSection(sec || 'general', anchor)
  requestAnimationFrame(() => {
    const selected = setOv.querySelector('.dlg-n.on')
    if (selected) selected.focus()
  })
  // 面板内容归 React，但输入框的模型选择器仍读 setData，所以这里补一次。
  // 放在 showSection 之后：先让面板出现，再回填选择器。
  setData = await collect('settings_get').catch(() => null)
  window.dispatchEvent(new Event('wyckoff:models-changed'))
}

/** Load appearance before the user opens settings, so launch honours it. */
async function loadAppearance () {
  setData = await collect('settings_get').catch(() => null)
  applyAppearance(setData)
  // 同一份数据里就有 models / default_model，顺手把选择器画上，
  // 不必为它再发一次 settings_get。
  window.dispatchEvent(new Event('wyckoff:models-changed'))
}

for (const btn of document.querySelectorAll('.dlg-n')) {
  btn.onclick = () => showSection(btn.dataset.sec)
}
document.getElementById('set-close').onclick = closeSettings
// Backdrop click closes; clicks inside the dialog must not bubble out to it.
setOv.onclick = (e) => { if (e.target === setOv) closeSettings() }
setOv.addEventListener('keydown', (event) => {
  if (event.key !== 'Tab') return
  const items = focusableWithin(setOv)
  if (!items.length) return
  const first = items[0]
  const last = items[items.length - 1]
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
})

// Destination pages render into the main area. Previously every nav item threw
// a panel into the right-hand artifact pane, which turned that pane into a
// dumping ground and left the main area showing an unrelated conversation.
// title/sub are i18n keys, not literal text, so a language switch re-resolves
// them on the next render rather than freezing whatever language loaded first.
const PAGES = {
  // 三页都由 React 渲染，各自管自己的请求与刷新。
  tasks: {
    titleKey: 'tasks.heading',
    subKey: 'tasks.pageSub',
    wide: true,
    build: async () => window.WyckoffReact.tasksPage()
  },
  approvals: {
    titleKey: 'nav.approvals',
    subKey: 'approvals.pageSub',
    build: async () => window.WyckoffReact.approvalsPage()
  },
  schedules: {
    titleKey: 'schedules.heading',
    subKey: 'schedules.pageSub',
    build: async () => window.WyckoffReact.schedulesPage()
  },
  portfolio: {
    titleKey: 'tab.charts',
    subKey: 'portfolio.pageSub',
    wide: true,
    // React 接管：加了本地缓存与行内增删改，图表仍由 charts.js 生成。
    build: async () => window.WyckoffReact.portfolioPage()
  },
  reports: {
    titleKey: 'nav.reports',
    subKey: 'reports.pageSub',
    wide: true,
    build: buildReportPage
  },
  // 这两页由 React 渲染。它们返回的 { node, dispose } 正是 showPage 已有的
  // 约定，dispose 用来 unmount React root —— 不卸载会在页面切换后泄漏。
  tracking: {
    titleKey: 'tracking.heading',
    subKey: 'tracking.pageSub',
    wide: true,
    build: async () => window.WyckoffReact.trackingPage()
  },
  attribution: {
    titleKey: 'attribution.heading',
    subKey: 'attribution.pageSub',
    build: async () => window.WyckoffReact.attributionPage()
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
  const selected = document.querySelector(`.nv[data-view="${view}"] [data-i18n]`)
  if (selected) {
    viewTitle.dataset.i18n = selected.dataset.i18n
    viewTitle.textContent = t(selected.dataset.i18n)
  }
  if (view === 'chat') showChat()
  else showPage(view)
}

for (const nav of document.querySelectorAll('.nv')) nav.onclick = () => navigateView(nav.dataset.view)
selectNav('chat')

document.getElementById('btn-new-analysis').onclick = () => {
  navigateView('chat')
}

btnRestart.onclick = () => window.wyckoff.restart()

// Reports, browser and the artifact pane are content the agent produces, not
// navigation. They live behind a labelled "打开" menu rather than a row of
// glyphs nobody could decode.
const openBtn = document.getElementById('btn-open')
const openMenu = document.getElementById('open-menu')

const visibleMenuItems = (menu) => [...menu.querySelectorAll('[role="menuitem"]')]
  .filter((item) => !item.hidden && !item.disabled)

function handleMenuKeys (event, menu, trigger, close) {
  const items = visibleMenuItems(menu)
  const current = items.indexOf(document.activeElement)
  let target = -1
  if (event.key === 'ArrowDown') target = current < 0 ? 0 : (current + 1) % items.length
  else if (event.key === 'ArrowUp') target = current < 0 ? items.length - 1 : (current - 1 + items.length) % items.length
  else if (event.key === 'Home') target = 0
  else if (event.key === 'End') target = items.length - 1
  else if (event.key === 'Escape') {
    event.preventDefault()
    close()
    trigger.focus()
    return
  } else if (event.key === 'Tab') {
    close()
    return
  } else return
  if (items[target]) {
    event.preventDefault()
    items[target].focus()
  }
}

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
  const first = visibleMenuItems(openMenu)[0]
  if (first) first.focus()
}

openBtn.onclick = (e) => {
  e.stopPropagation()
  setOpenMenu(openMenu.hidden)
}
openBtn.onkeydown = (event) => {
  if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return
  event.preventDefault()
  setOpenMenu(true)
  const items = visibleMenuItems(openMenu)
  const target = event.key === 'ArrowUp' ? items[items.length - 1] : items[0]
  if (target) target.focus()
}
openMenu.addEventListener('keydown', (event) => handleMenuKeys(event, openMenu, openBtn, () => setOpenMenu(false)))

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
}

const menuAction = (fn) => () => { setOpenMenu(false); fn() }
document.getElementById('mi-chart').onclick = menuAction(openSymBox)
document.getElementById('mi-reports').onclick = menuAction(() => navigateView('reports'))
document.getElementById('mi-browser').onclick = menuAction(openBrowser)
document.getElementById('mi-pane').onclick = menuAction(() => togglePane())
// The chip is a shortcut to the approvals page; keep the sidebar in sync so
// the highlighted nav item always matches what is on screen.
document.getElementById('pending-chip').onclick = () => {
  navigateView('approvals')
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
  const first = visibleMenuItems(acctMenu)[0]
  if (first) first.focus()
}

// 上一次看到的账号。用来发现「换人了」——包括登录、退出、以及换个账号登录。
let lastUserId = null

async function loadAccount () {
  const data = await collect('account').catch(() => null)
  signedIn = Boolean(data && data.signed_in)
  const email = (data && data.email) || ''
  acctEmail = email

  // 账号变了就清掉持仓缓存。挂在这里而不是只挂退出：登录、换账号登录都会走
  // loadAccount，只在退出时清会漏掉「A 没退直接登 B」这种路径。
  const uid = String((data && data.user_id) || '')
  if (lastUserId !== null && lastUserId !== uid) {
    const react = window.WyckoffReact
    if (react && react.clearPortfolioCaches) react.clearPortfolioCaches()
    // 还要通知已经挂载的页面把 state 也清掉。只清缓存挡得住「下次进页面」，
    // 挡不住「此刻正看着持仓页」—— 在持仓页上开设置退出登录，关掉设置后
    // 屏幕上还是上一个账号的仓位。
    window.dispatchEvent(new CustomEvent('wyckoff:account-changed', { detail: { userId: uid } }))
  }
  lastUserId = uid
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
acctBtn.onkeydown = (event) => {
  if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return
  event.preventDefault()
  setMenu(true)
  const items = visibleMenuItems(acctMenu)
  const target = event.key === 'ArrowUp' ? items[items.length - 1] : items[0]
  if (target) target.focus()
}
acctMenu.addEventListener('keydown', (event) => handleMenuKeys(event, acctMenu, acctBtn, () => setMenu(false)))

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
  // 退出即清掉所有账号的持仓缓存。缓存虽然已经按 user_id 分区，但登录态一变
  // 就该清空：留着等于把上一个人的持仓存在这台机器上，换个账号进来就可能看到。
  const react = window.WyckoffReact
  if (react && react.clearPortfolioCaches) react.clearPortfolioCaches()
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

sideButton.onclick = () =>
  setSide(win.classList.contains('side-off'))

const togglePane = () => {
  if (win.classList.contains('pane-on')) {
    // Collapsing is reversible: Codex keeps the current artifact around rather
    // than making a layout control silently destroy the user's tabs.
    setPane(false)
    return
  }
  if (!pane.count()) return
  setPane(true)
  pane.showActive()
}

document.getElementById('btn-pane').onclick = () => setPane(false)

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

// React 侧需要用到的、仍归 app.js 拥有的动作：系统消息要进对话流，
// 退出登录要刷新侧栏并关掉设置面板。
//
// 不能写成 `if (window.WyckoffReact) setHooks(...)`：app.js 是普通脚本，会在
// type="module" 的 main.tsx 之前跑完，那个判断恒为假，于是 React 一直用着
// 默认的空函数 —— 表现为设置页「退出登录」没反应、模型改了输入区不刷新。
//
// 改成把 hooks 挂到约定的全局上：谁先就绪都不影响。app.js 先跑就留在这里等
// React 取；万一将来 React 先就绪，下面那次 setHooks 直接生效。
const REACT_HOOKS = {
  onMessage: (text, isError) => sysLine(text, Boolean(isError)),
  onSignOut: () => { closeSettings(); doSignOut() },
  onConfigChanged: () => { refreshModels() }
}
/**
 * React 页面要用到的、仍归 app.js 拥有的动作。
 *
 * 与 REACT_HOOKS 同理，挂全局而不是靠「对方已就绪」——两个脚本的执行顺序由
 * type="module" 决定，靠假设接线会静默失效。
 */
window.WyckoffApp = {
  navigate: (view) => navigateView(view),
  refreshApprovals: () => { void refreshApprovals() },
  refreshSchedules: () => { void refreshSchedules() },
  openKline: (code) => openKline(String(code)),
  openReport: (title, body) => openReport(title, body, new Date().toLocaleString(i18n.getLang())),
  refreshCharts: (codes) => refreshDrawnCharts(new Set(codes || [])),
  openSettings: (section, anchor) => { void openSettings(section, anchor) },
  sysLine: (text, isError) => sysLine(text, Boolean(isError)),
  getSendOnEnter: () => sendOnEnter
}

window.WyckoffPendingHooks = REACT_HOOKS
if (window.WyckoffReact && window.WyckoffReact.setHooks) {
  window.WyckoffReact.setHooks(REACT_HOOKS)
}

// Paint the static HTML in the resolved language before anything else shows.
i18n.applyDom()

// A language switch re-renders everything dynamic: static nodes are handled by
// applyDom inside i18n.setLang; here we repaint the JS-built surfaces.
i18n.onChange(() => {
  // The header chip embeds a translated word ("待批"/"pending"); repaint it.
  refreshApprovals()
  // 选择器里的「未配置模型」「新增模型」都是译文。
  window.dispatchEvent(new Event('wyckoff:models-changed'))
  // The open settings section (labels, options, the language row itself).
  if (!setOv.hidden) {
    const active = document.querySelector('.dlg-n.on')
    showSection(active ? active.dataset.sec : 'general')
  }
  // The active destination page (title, subtitle, body).
  if (activePage) showPage(activePage)
})

// 会话区整块交给 React（欢迎页 + 对话流 + 输入区）。
if (window.WyckoffReact && window.WyckoffReact.mountChat) {
  window.WyckoffReact.mountChat(document.getElementById('stream'))
} else {
  // React 尚未就绪（普通脚本先跑）—— 留个信号让它自己来接。
  window.WyckoffPendingChatHost = document.getElementById('stream')
}

window.wyckoff.onStatus(setStatus)

// Python may have reached ready before this listener existed; pull the current
// status so the UI does not sit on "连接中…" with no calls ever issued.
window.wyckoff.status().then(setStatus).catch(() => {})
