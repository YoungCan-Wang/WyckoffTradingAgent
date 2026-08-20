'use strict'

/**
 * Canvas 量价图：主图蜡烛 + 成交量副图。
 *
 * 为什么是 canvas 而不是 SVG（renderer 其余部分都是 SVG）：几百根 K 线用 SVG
 * 就是几千个 DOM 节点，缩放平移会掉帧；而标注层的防重叠与文字测量算法本身
 * 也是 canvas 2D 的。
 *
 * 对外的坐标契约是 timeToX / locateTime / priceToY。标注层只依赖它们，不关心
 * 图怎么画，所以换渲染方式不会牵动标注。
 *
 * A 股约定：红涨绿跌（与美股相反）。颜色从 CSS 变量读 —— canvas 认不了
 * var()，必须先用 getComputedStyle 解析成字面色。
 */

;(function () {
const PAD = { left: 8, right: 56, top: 10, bottom: 18 }
const VOL_RATIO = 0.22
const VOL_GAP = 10
const MIN_SPAN = 20
const DEFAULT_SPAN = 120

/** canvas 读不到 CSS 变量，开图时解析一次成字面色。 */
function readTheme () {
  const cs = getComputedStyle(document.documentElement)
  const pick = (name, fallback) => (cs.getPropertyValue(name) || '').trim() || fallback
  return {
    up: pick('--up', '#e5484d'),
    dn: pick('--dn', '#2f9e5f'),
    ink: pick('--tx', '#26251f'),
    ink2: pick('--tx2', '#6f6d66'),
    ink3: pick('--tx3', '#9b9891'),
    line: pick('--line', '#e6e4de'),
    grid: pick('--line2', '#efede8'),
    paper: pick('--paper', '#fffefc'),
    accent: pick('--clay', '#d97757')
  }
}

const fmtPrice = (v) => (Math.abs(v) >= 100 ? v.toFixed(1) : v.toFixed(2))

function fmtVol (v) {
  if (v >= 1e8) return `${(v / 1e8).toFixed(1)}亿`
  if (v >= 1e4) return `${(v / 1e4).toFixed(0)}万`
  return String(Math.round(v || 0))
}

/**
 * @param {{bars: object}} opts bars 为列式 {date,open,high,low,close,volume}
 * @returns {{node: HTMLElement, draw: Function, dispose: Function,
 *            addPainter: Function, timeToX: Function, locateTime: Function,
 *            priceToY: Function,
 *            visibleRange: Function}}
 */
function createKlineChart (opts) {
  const bars = (opts && opts.bars) || {}
  const count = (bars.close || []).length

  const node = document.createElement('div')
  node.className = 'kl'
  const canvas = document.createElement('canvas')
  canvas.className = 'kl-cv'
  const readout = document.createElement('div')
  readout.className = 'kl-read'
  node.append(canvas, readout)

  const ctx = canvas.getContext('2d')
  let theme = readTheme()
  let W = 0
  let H = 0
  let from = Math.max(0, count - DEFAULT_SPAN)
  let to = count
  let hover = -1
  const painters = []

  // 日期 → 行号：标注按日期定位，图按行号定位，这里桥接一次。
  const indexOfDate = new Map()
  for (let i = 0; i < count; i++) indexOfDate.set(String(bars.date[i]), i)

  const mainBox = () => {
    const h = Math.max(20, (H - PAD.top - PAD.bottom - VOL_GAP) * (1 - VOL_RATIO))
    return { x: PAD.left, y: PAD.top, w: Math.max(10, W - PAD.left - PAD.right), h }
  }

  const volBox = () => {
    const m = mainBox()
    const y = m.y + m.h + VOL_GAP
    return { x: m.x, y, w: m.w, h: Math.max(10, H - PAD.bottom - y) }
  }

  /** 可见区间价格范围，留 4% 呼吸空间免得贴边。 */
  function priceSpan () {
    let lo = Infinity
    let hi = -Infinity
    for (let i = from; i < to; i++) {
      const a = bars.high[i]
      const b = bars.low[i]
      if (a != null && a > hi) hi = a
      if (b != null && b < lo) lo = b
    }
    // 可见区间价格全相等（一字板、停牌横盘）时 hi === lo，没有区间可言。
    // 原来返回 {lo:0, hi:1}：**丢掉了真实价位**，于是 priceToY(10) 会算成
    // m.y + (1 - 10/1) * m.h —— 远在画布上方，整屏 K 线消失，看起来像没数据。
    // 改成围绕真实价格造一个人工区间，图上会显示一条水平的一字线，价格轴上的
    // 刻度也仍然是真的。
    if (!(lo < hi)) {
      const level = isFinite(lo) ? lo : (isFinite(hi) ? hi : 0)
      // 用 1% 而不是固定值：不同价位的票（3 元 / 1500 元）都要得到合理的留白。
      const span = Math.max(Math.abs(level) * 0.01, 0.01)
      return { lo: level - span, hi: level + span }
    }
    const pad = (hi - lo) * 0.04
    return { lo: lo - pad, hi: hi + pad }
  }

  function maxVol () {
    let hi = 0
    for (let i = from; i < to; i++) {
      const v = bars.volume ? bars.volume[i] : 0
      if (v != null && v > hi) hi = v
    }
    return hi || 1
  }

  // ---- 坐标契约 -----------------------------------------------------------
  const step = () => mainBox().w / Math.max(1, to - from)

  const barToX = (i) => (i < from || i >= to ? null : mainBox().x + (i - from + 0.5) * step())

  const priceToY = (price) => {
    if (price == null || !isFinite(price)) return null
    const m = mainBox()
    const { lo, hi } = priceSpan()
    return m.y + (1 - (price - lo) / (hi - lo)) * m.h
  }

  const timeToX = (date) => {
    const i = indexOfDate.get(String(date))
    return i === undefined ? null : barToX(i)
  }

  /**
   * 定位一个日期，并且说清楚「没有坐标」是哪一种没有。
   *
   * timeToX 把三件事压成同一个 null：日期不在数据集里、K 线在可见区间左边、
   * 在右边。调用方只能猜，而标注绘制猜错了就会画出横贯全图的幻影矩形
   * （两端都在左侧屏外时，start 被当成「出左边」夹到左边界、end 被当成
   * 「出右边」夹到右边界）。
   *
   * @returns {{ x: number|null, side: 'in'|'left'|'right'|'unknown' }}
   */
  const locateTime = (date) => {
    const i = indexOfDate.get(String(date))
    if (i === undefined) return { x: null, projectedX: null, side: 'unknown' }
    // 屏外那一头的**真实**坐标（可以是负数或超出右边界）。趋势线要靠它保持
    // 正确的角度：把出屏的端点夹到边界会把线掰弯，而趋势线的用途恰恰是延伸
    // 到屏外。画布本身会裁剪，不需要我们先夹。
    const projectedX = mainBox().x + (i - from + 0.5) * step()
    if (i < from) return { x: null, projectedX, side: 'left' }
    if (i >= to) return { x: null, projectedX, side: 'right' }
    return { x: projectedX, projectedX, side: 'in' }
  }

  const visibleRange = () => ({ from, to })

  const xToBar = (x) => {
    const m = mainBox()
    const i = Math.floor((x - m.x) / step()) + from
    return i >= from && i < to ? i : -1
  }

  // ---- 绘制 ---------------------------------------------------------------
  function drawGrid () {
    const m = mainBox()
    const { lo, hi } = priceSpan()
    ctx.strokeStyle = theme.grid
    ctx.fillStyle = theme.ink3
    ctx.lineWidth = 1
    ctx.font = '10px ui-monospace, Menlo, monospace'
    ctx.textAlign = 'left'
    ctx.textBaseline = 'middle'
    for (let k = 0; k <= 4; k++) {
      const y = Math.round(m.y + (m.h * k) / 4) + 0.5
      ctx.beginPath()
      ctx.moveTo(m.x, y)
      ctx.lineTo(m.x + m.w, y)
      ctx.stroke()
      ctx.fillText(fmtPrice(hi - ((hi - lo) * k) / 4), m.x + m.w + 6, y)
    }
  }

  function drawCandles () {
    const m = mainBox()
    const s = step()
    const body = Math.max(1, Math.min(14, s * 0.68))
    for (let i = from; i < to; i++) {
      const o = bars.open[i]
      const c = bars.close[i]
      const h = bars.high[i]
      const l = bars.low[i]
      if (o == null || c == null || h == null || l == null) continue
      const x = m.x + (i - from + 0.5) * s
      const rising = c >= o
      const color = rising ? theme.up : theme.dn
      ctx.strokeStyle = color
      ctx.fillStyle = color
      // 影线
      ctx.lineWidth = 1
      ctx.beginPath()
      ctx.moveTo(Math.round(x) + 0.5, priceToY(h))
      ctx.lineTo(Math.round(x) + 0.5, priceToY(l))
      ctx.stroke()
      // 实体：一字线（开收相等）至少画 1px，否则整根消失
      const yO = priceToY(o)
      const yC = priceToY(c)
      const top = Math.min(yO, yC)
      const hgt = Math.max(1, Math.abs(yC - yO))
      ctx.fillRect(x - body / 2, top, body, hgt)
    }
  }

  function drawVolume () {
    const v = volBox()
    const s = step()
    const body = Math.max(1, Math.min(14, s * 0.68))
    const top = maxVol()
    ctx.strokeStyle = theme.grid
    ctx.beginPath()
    ctx.moveTo(v.x, Math.round(v.y + v.h) + 0.5)
    ctx.lineTo(v.x + v.w, Math.round(v.y + v.h) + 0.5)
    ctx.stroke()
    for (let i = from; i < to; i++) {
      const vol = bars.volume ? bars.volume[i] : null
      if (vol == null) continue
      const o = bars.open[i]
      const c = bars.close[i]
      ctx.fillStyle = c >= o ? theme.up : theme.dn
      const bh = Math.max(1, (vol / top) * v.h)
      const x = v.x + (i - from + 0.5) * s
      ctx.globalAlpha = 0.55
      ctx.fillRect(x - body / 2, v.y + v.h - bh, body, bh)
      ctx.globalAlpha = 1
    }
    ctx.fillStyle = theme.ink3
    ctx.font = '10px ui-monospace, Menlo, monospace'
    ctx.textAlign = 'left'
    ctx.textBaseline = 'top'
    ctx.fillText(fmtVol(top), v.x + v.w + 6, v.y)
  }

  function drawDates () {
    const m = mainBox()
    const s = step()
    ctx.fillStyle = theme.ink3
    ctx.font = '10px ui-monospace, Menlo, monospace'
    ctx.textAlign = 'center'
    ctx.textBaseline = 'top'
    // 目标约 6 个刻度，取整到 K 线行号避免标签抖动。
    const stride = Math.max(1, Math.round((to - from) / 6))
    for (let i = from; i < to; i += stride) {
      const label = String(bars.date[i] || '').slice(5)
      ctx.fillText(label, m.x + (i - from + 0.5) * s, H - PAD.bottom + 3)
    }
  }

  function drawCrosshair () {
    if (hover < from || hover >= to) return
    const m = mainBox()
    const x = Math.round(m.x + (hover - from + 0.5) * step()) + 0.5
    ctx.save()
    ctx.strokeStyle = theme.ink3
    ctx.setLineDash([3, 3])
    ctx.beginPath()
    ctx.moveTo(x, m.y)
    ctx.lineTo(x, volBox().y + volBox().h)
    ctx.stroke()
    ctx.restore()
  }

  function draw () {
    if (!W || !H) return
    ctx.clearRect(0, 0, W, H)
    ctx.fillStyle = theme.paper
    ctx.fillRect(0, 0, W, H)
    if (!count) return
    drawGrid()
    drawCandles()
    drawVolume()
    drawDates()
    // 标注在蜡烛之上、十字线之下。
    for (const paint of painters) {
      try {
        paint({ ctx, width: W, height: H, theme, mainBox: mainBox(), timeToX, locateTime, priceToY, visibleRange })
      } catch (err) {
        // 一个标注画错不该让整张图空掉。
        console.error('[kline] painter failed:', err && err.message)
      }
    }
    drawCrosshair()
  }

  function syncReadout () {
    if (hover < 0 || hover >= count) {
      readout.textContent = ''
      return
    }
    const pct = bars.pct_chg ? bars.pct_chg[hover] : null
    const cls = (bars.close[hover] >= bars.open[hover]) ? 'up' : 'dn'
    readout.replaceChildren()
    const add = (text, extra) => {
      const span = document.createElement('span')
      span.className = extra || ''
      span.textContent = text
      readout.appendChild(span)
    }
    add(String(bars.date[hover] || ''))
    add(`开 ${fmtPrice(bars.open[hover])}`)
    add(`高 ${fmtPrice(bars.high[hover])}`)
    add(`低 ${fmtPrice(bars.low[hover])}`)
    add(`收 ${fmtPrice(bars.close[hover])}`, cls)
    if (pct != null) add(`${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`, cls)
    add(`量 ${fmtVol(bars.volume ? bars.volume[hover] : 0)}`)
  }

  // ---- 尺寸与交互 ---------------------------------------------------------
  function resize () {
    const rect = node.getBoundingClientRect()
    if (rect.width < 2 || rect.height < 2) return
    const dpr = window.devicePixelRatio || 1
    W = rect.width
    H = rect.height
    // canvas 的位图尺寸按 DPR 放大，再把绘制坐标系缩回 CSS 像素。
    canvas.width = Math.round(W * dpr)
    canvas.height = Math.round(H * dpr)
    canvas.style.width = `${W}px`
    canvas.style.height = `${H}px`
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    draw()
  }

  const observer = new ResizeObserver(resize)
  observer.observe(node)

  const onWheel = (event) => {
    if (!count) return
    event.preventDefault()
    const span = to - from
    // 以指针所在位置为锚缩放，手感才对。
    const anchor = xToBar(event.offsetX)
    const pivot = anchor >= 0 ? anchor : from + Math.floor(span / 2)
    const factor = event.deltaY > 0 ? 1.15 : 1 / 1.15
    let next = Math.round(span * factor)
    next = Math.max(MIN_SPAN, Math.min(count, next))
    const ratio = span > 0 ? (pivot - from) / span : 0.5
    let nf = Math.round(pivot - ratio * next)
    nf = Math.max(0, Math.min(count - next, nf))
    from = nf
    to = nf + next
    draw()
  }

  let dragX = null
  const onDown = (event) => { dragX = event.offsetX }
  const onUp = () => { dragX = null }
  const onMove = (event) => {
    if (dragX != null && count) {
      const moved = Math.round((dragX - event.offsetX) / step())
      if (moved !== 0) {
        const span = to - from
        let nf = Math.max(0, Math.min(count - span, from + moved))
        from = nf
        to = nf + span
        dragX = event.offsetX
      }
    }
    hover = xToBar(event.offsetX)
    syncReadout()
    draw()
  }
  const onLeave = () => { hover = -1; syncReadout(); draw() }

  canvas.addEventListener('wheel', onWheel, { passive: false })
  canvas.addEventListener('mousedown', onDown)
  window.addEventListener('mouseup', onUp)
  canvas.addEventListener('mousemove', onMove)
  canvas.addEventListener('mouseleave', onLeave)

  const dispose = () => {
    observer.disconnect()
    canvas.removeEventListener('wheel', onWheel)
    canvas.removeEventListener('mousedown', onDown)
    window.removeEventListener('mouseup', onUp)
    canvas.removeEventListener('mousemove', onMove)
    canvas.removeEventListener('mouseleave', onLeave)
    painters.length = 0
  }

  // 主题切换（明暗）后颜色要重解析，否则暗色下还在用浅色墨。
  const refreshTheme = () => { theme = readTheme(); draw() }

  return {
    node,
    draw,
    resize,
    dispose,
    refreshTheme,
    addPainter: (fn) => { painters.push(fn); draw() },
    timeToX,
    locateTime,
    priceToY,
    visibleRange
  }
}

window.WyckoffKline = { createKlineChart, readTheme, fmtPrice, fmtVol }
})()
