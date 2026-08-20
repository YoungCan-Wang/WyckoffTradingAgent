'use strict'

/**
 * 命令式外壳：产物面板、内置浏览器、K 线图、面板宽度、外观。
 *
 * 这些**刻意不用 React**：
 * - 图表是 canvas 绘制，浏览器是浮在窗口上的原生 view（靠 rect 上报几何），
 *   报告容器是 sandbox iframe（安全边界）—— 套一层 React 只会多一层壳，
 *   真正的绘制仍然是命令式的。
 * - 面板拖拽写的是 CSS 变量 + pointer capture，用 state 表达会引入一帧延迟。
 *
 * React 侧通过 window.WyckoffShell 调用这里；反向靠 window.WyckoffApp。
 */
const i18n = window.WyckoffI18n
const t = (key, params) => i18n.t(key, params)

const win = document.querySelector('.win')
const paneBody = document.getElementById('pane-body')
const paneResizer = document.getElementById('pane-resizer')
let browserBox = null
let browserObserver = null
let setData = null
let sendOnEnter = true

// 面板宽度：记住用户拖到的宽度，但两侧都留出可用的最小值 —— 拖到极限时
// 会话区不该被压成一条缝。
const PANE_WIDTH_KEY = 'wyckoff.pane.width'
const MIN_PANE_WIDTH = 360
const MIN_THREAD_WIDTH = 420


// ---- 面板宽度与侧栏 --------------------------------------------------------

function setPane (on) {
  win.classList.toggle('pane-on', Boolean(on))
  // The browser is a native view floating above the DOM; hiding the pane must
  // detach it or it stays visible over the conversation.
  if (!on) window.wyckoff.browser.hide()
  if (on) requestAnimationFrame(syncBrowserBounds)
}

function paneWidthLimit () {
  // #side 由 React 渲染，而且收起时整个节点不存在 —— 取不到就当 0。
  const side = document.getElementById('side')
  const sideWidth = side ? side.offsetWidth : 0
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

setPane(false)
restorePaneWidth()


const el = (tag, cls, text) => {
  const node = document.createElement(tag)
  if (cls) node.className = cls
  if (text !== undefined) node.textContent = text
  return node
}


// Messages go into .inner, which is width-capped and centred; #stream itself


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



// 产物面板的可见性由内容驱动：有东西就出现，最后一个页签关掉就消失。
// 每条打开路径都免费获得这个行为，不用各自 toggle。
const pane = new window.WyckoffTabs.TabPane('tabs', 'pane-body', {
  onCountChange: (count) => {
    setPane(count > 0)
    // 「打开」菜单里的「产物面板」项要跟着有无内容显隐 —— 那个菜单归 React。
    window.dispatchEvent(new CustomEvent('wyckoff:artifacts', { detail: { count } }))
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
        if (!res.ok) window.WyckoffApp?.sysLine?.(t('browser.openFailed', { error: res.error }), true)
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


// ---- 外观 ------------------------------------------------------------------

const root = document.documentElement
const osDark = window.matchMedia('(prefers-color-scheme: dark)')

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





// ---- 手动打开 K 线图 -------------------------------------------------------
// 图表原本只有 agent 能开（annotate_chart 执行时顺带开一个 tab），所以用户想
// 单纯看某只票的图，得先设法让模型去标注它。这里补上直接的入口。

let symBox = null

function closeSymBox () {
  // 按 DOM 查而不是只看 symBox：浮层要到下一帧才登记，这中间也得能关掉。
  for (const node of document.querySelectorAll('.symbox')) node.remove()
  symBox = null
}

function openSymBox (anchor) {
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
  // 锚点由 React 传进来 —— 顶栏那个「打开」按钮现在是 React 组件，
  // 这里引用不到。拿不到锚点时退到顶栏下方居中，别抛错。
  const r = anchor && anchor.getBoundingClientRect
    ? anchor.getBoundingClientRect()
    : { right: window.innerWidth / 2 + box.offsetWidth / 2, bottom: 56 }
  const left = Math.min(r.right - box.offsetWidth, window.innerWidth - box.offsetWidth - 8)
  box.style.left = `${Math.max(8, left)}px`
  box.style.top = `${r.bottom + 6}px`
  input.focus()
  symBox = box
  armSymBoxDismiss()
}

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



// ---- 暴露给 React ----------------------------------------------------------

/**
 * 产物面板与绘图的入口。React 侧只调用它，不碰这些模块的内部 ——
 * 它们各自管着 canvas 上下文、原生 view 句柄或 iframe，跨边界操作会漏资源。
 */
window.WyckoffShell = {
  openReport: (title, body) => openReport(title, body, new Date().toLocaleString(i18n.getLang())),
  openKline: (symbol) => openKline(String(symbol)),
  refreshCharts: (codes) => refreshDrawnCharts(new Set(codes || [])),
  promptKline: (anchor) => openSymBox(anchor),
  openBrowser: () => openBrowser(),
  togglePane: () => togglePane(),
  syncBrowser: () => syncBrowserBounds(),
  getSendOnEnter: () => sendOnEnter,
  /** 报告库那一页的内容由命令式 viewer 构建（sandbox iframe 是安全边界）。 */
  buildReportPage: async () => {
    const viewer = window.createArtifactViewer({
      call: collect,
      onError: (message) => window.WyckoffApp?.sysLine?.(message, true)
    })
    await viewer.refresh()
    const wrap = el('div', 'report-page')
    wrap.appendChild(viewer.node)
    return { node: wrap, dispose: viewer.dispose }
  },
  /** 启动时与设置改动后都要应用外观。 */
  loadAppearance: async () => {
    setData = await collect('settings_get').catch(() => null)
    applyAppearance(setData)
  }
}

// 跟随系统就要响应它在运行期间的变化。
osDark.addEventListener('change', () => {
  if (!setData || (setData.desktop_appearance || 'system') === 'system') applyAppearance(setData)
})

// 面板宽度：窗口变化时重新夹一次，并同步原生 view 的几何。
window.addEventListener('resize', () => {
  const paneWidth = document.getElementById('pane').getBoundingClientRect().width
  if (paneWidth) setPaneWidth(paneWidth, false)
  syncBrowserBounds()
})

document.getElementById('btn-pane').onclick = () => setPane(false)

restorePaneWidth()
setPane(false)

// ⌘K / ⌘T / ⌘⌥B：这三个开的都是命令式面板，所以快捷键留在这里。
// ⌘B（侧栏）、⌘R（报告库）、⌘,（设置）归 React。
window.addEventListener('keydown', (e) => {
  if (!(e.metaKey || e.ctrlKey)) return
  const key = e.key.toLowerCase()
  if (key === 'b' && e.altKey) { e.preventDefault(); togglePane() }
  else if (key === 't' && !e.altKey) { e.preventDefault(); openBrowser() }
  else if (key === 'k' && !e.altKey) { e.preventDefault(); openSymBox() }
})

/*
 * 点外面关掉 K 线输入浮层。
 *
 * 注册在下一帧：打开浮层的那次 click 还在冒泡，同一轮里挂监听会立刻收到它，
 * 于是浮层刚建好就被自己关掉 —— 表现是「点 K 线图毫无反应」。
 * （排除浮层自身仍然要做：里面的输入框和按钮点了不该关。）
 */
function armSymBoxDismiss () {
  const onDocClick = (e) => {
    if (e.target.closest && e.target.closest('.symbox')) return
    closeSymBox()
    window.removeEventListener('click', onDocClick)
  }
  requestAnimationFrame(() => window.addEventListener('click', onDocClick))
}
