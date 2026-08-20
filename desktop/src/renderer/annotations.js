'use strict'

/**
 * 图表标注层：把威科夫结构画到 K 线图上。
 *
 * 只依赖 kline.js 暴露的两个坐标函数（timeToX / priceToY），不碰图的内部，
 * 所以换渲染方式不牵动这里。
 *
 * 两个关键设计（都是为了"暗色图上仍然读得清"）：
 *
 * 1. 标签不刷标注本身的颜色 —— 颜色降级成一个 3px 小圆点，文字用高对比墨色
 *    画在磨砂圆角矩形上。把任意颜色直接刷在深色底上，很容易糊成一团。
 * 2. 芯片防重叠：同一价位堆几个标注时，标签会互相压住。按锚点排序后贪心
 *    下推，只动 y 不动 x —— x 是有意义的轴（时间），挪了就指错 K 线。
 */

;(function () {
const CHIP_H = 16
const CHIP_GAP = 4
const CHIP_PAD = 6
const DOT = 3
const EDGE = 4
const FONT = '10.5px -apple-system, "PingFang SC", sans-serif'

// 事件类型 → 颜色角色 + 标签 key。颜色只用于圆点，不用于文字。
const KIND_STYLE = {
  spring: { role: 'up', key: 'kline.kindSpring' },
  upthrust: { role: 'dn', key: 'kline.kindUpthrust' },
  sos: { role: 'up', key: 'kline.kindSos' },
  lps: { role: 'accent', key: 'kline.kindLps' },
  evr: { role: 'accent', key: 'kline.kindEvr' }
}

const t = (key, params) => window.WyckoffI18n.t(key, params)

// 文字宽度测量缓存：不缓存的话每次平移/缩放都要重新测一遍所有标签。
const widthCache = new Map()
function measure (ctx, text) {
  const hit = widthCache.get(text)
  if (hit !== undefined) return hit
  const w = ctx.measureText(text).width
  if (widthCache.size > 512) widthCache.clear()
  widthCache.set(text, w)
  return w
}

function roundRect (ctx, x, y, w, h, r) {
  const rad = Math.min(r, h / 2, w / 2)
  ctx.beginPath()
  ctx.moveTo(x + rad, y)
  ctx.arcTo(x + w, y, x + w, y + h, rad)
  ctx.arcTo(x + w, y + h, x, y + h, rad)
  ctx.arcTo(x, y + h, x, y, rad)
  ctx.arcTo(x, y, x + w, y, rad)
  ctx.closePath()
}

const isDark = () => document.documentElement.classList.contains('dark')

/** 磨砂底 + 高对比墨色文字。深色/浅色各一套，不跟随标注色。 */
function chipSkin () {
  return isDark()
    ? { fill: 'rgba(22, 24, 29, .92)', ink: '#e6e8ec', border: 'rgba(255,255,255,.14)' }
    : { fill: 'rgba(255, 254, 252, .94)', ink: '#26251f', border: 'rgba(0,0,0,.12)' }
}

const chipWidth = (ctx, text) => CHIP_PAD * 2 + DOT * 2 + 4 + measure(ctx, text)

/**
 * 贪心防重叠：按 top 排序，逐个与已定位的芯片比较，x 区间相交就下推。
 * 只改 y —— x 承载时间语义，挪动会让标签指向错误的 K 线。
 */
function layoutChips (chips, height) {
  const placed = []
  for (const chip of [...chips].sort((a, b) => a.top - b.top)) {
    let top = chip.top
    for (const prev of placed) {
      const overlapX = chip.left < prev.left + prev.width + CHIP_GAP &&
        prev.left < chip.left + chip.width + CHIP_GAP
      if (overlapX && top < prev.top + CHIP_H + CHIP_GAP) {
        top = prev.top + CHIP_H + CHIP_GAP
      }
    }
    const bottomLimit = height - CHIP_H - EDGE
    if (top > bottomLimit) {
      // 到底了就改成往**上**让位，而不是夹回底边。
      //
      // 原来无条件 clamp 到 bottomLimit：底部挤了几个芯片时，它们全被夹到同一个
      // y，防重叠白做了 —— 而这恰恰是重叠最容易发生的地方（价格轴附近标注密集）。
      top = bottomLimit
      let guard = placed.length + 1
      while (guard-- > 0) {
        const hit = placed.find((prev) =>
          chip.left < prev.left + prev.width + CHIP_GAP &&
          prev.left < chip.left + chip.width + CHIP_GAP &&
          Math.abs(top - prev.top) < CHIP_H + CHIP_GAP
        )
        if (!hit) break
        top = hit.top - CHIP_H - CHIP_GAP
        // 上下都排不开就接受重叠 —— 画到框外更糟。
        if (top < EDGE) { top = EDGE; break }
      }
    }
    top = Math.max(EDGE, Math.min(bottomLimit, top))
    placed.push({ ...chip, top })
  }
  return placed
}

function drawChip (ctx, chip) {
  const skin = chipSkin()
  roundRect(ctx, chip.left, chip.top, chip.width, CHIP_H, 5)
  ctx.fillStyle = skin.fill
  ctx.fill()
  ctx.strokeStyle = skin.border
  ctx.lineWidth = 1
  ctx.stroke()
  // 颜色降级成小圆点：任意色刷成大色块在深色底上读不清。
  ctx.beginPath()
  ctx.arc(chip.left + CHIP_PAD + DOT, chip.top + CHIP_H / 2, DOT, 0, Math.PI * 2)
  ctx.fillStyle = chip.color
  ctx.fill()
  ctx.fillStyle = skin.ink
  ctx.font = FONT
  ctx.textAlign = 'left'
  ctx.textBaseline = 'middle'
  ctx.fillText(chip.text, chip.left + CHIP_PAD + DOT * 2 + 4, chip.top + CHIP_H / 2 + 0.5)
}

const colorOf = (theme, role) => theme[role] || theme.accent

/**
 * 生成一个 painter，交给 kline 的 addPainter。
 * @param {object} data { events, trading_range, targets }
 */
function createAnnotationPainter (data) {
  const events = (data && data.events) || []
  const range = (data && data.trading_range) || null
  const targets = (data && data.targets) || null
  const drawn = (data && data.annotations) || []

  return function paint (view) {
    const { ctx, width, theme, mainBox, timeToX, priceToY } = view
    const chips = []
    ctx.save()
    ctx.font = FONT
    // 裁剪到主图区：价格超出可见范围时 priceToY 会给出框外坐标（甚至负值），
    // 不裁剪就会画到成交量副图和头部去。
    ctx.beginPath()
    ctx.rect(mainBox.x, mainBox.y, mainBox.w, mainBox.h)
    ctx.clip()

    // 1) 交易区间：一个半透明矩形 + 上下边界线。威科夫的吸筹/派发区。
    if (range && range.support != null && range.resistance != null) {
      const yTop = priceToY(range.resistance)
      const yBot = priceToY(range.support)
      if (yTop != null && yBot != null) {
        ctx.fillStyle = withAlpha(theme.accent, 0.08)
        ctx.fillRect(mainBox.x, yTop, mainBox.w, Math.max(1, yBot - yTop))
        ctx.strokeStyle = withAlpha(theme.accent, 0.55)
        ctx.setLineDash([4, 3])
        ctx.lineWidth = 1
        for (const y of [yTop, yBot]) {
          ctx.beginPath()
          ctx.moveTo(mainBox.x, Math.round(y) + 0.5)
          ctx.lineTo(mainBox.x + mainBox.w, Math.round(y) + 0.5)
          ctx.stroke()
        }
        ctx.setLineDash([])
        chips.push(chipAt(ctx, mainBox.x + 6, yTop, t('kline.rangeTop'), theme.accent))
        chips.push(chipAt(ctx, mainBox.x + 6, yBot, t('kline.rangeBottom'), theme.accent))
      }
    }

    // 2) 目标位：水平虚线。保守/激进各一条，标出价格。
    if (targets) {
      const levels = [
        ['conservative', targets.conservative, 'kline.targetConservative'],
        ['aggressive', targets.aggressive, 'kline.targetAggressive']
      ]
      for (const [, price, key] of levels) {
        if (price == null) continue
        const y = priceToY(price)
        if (y == null || y < mainBox.y - 40 || y > mainBox.y + mainBox.h + 40) continue
        ctx.strokeStyle = withAlpha(theme.ink3, 0.75)
        ctx.setLineDash([2, 3])
        ctx.beginPath()
        ctx.moveTo(mainBox.x, Math.round(y) + 0.5)
        ctx.lineTo(mainBox.x + mainBox.w, Math.round(y) + 0.5)
        ctx.stroke()
        ctx.setLineDash([])
        const label = `${t(key)} ${window.WyckoffKline.fmtPrice(price)}`
        chips.push(chipAt(ctx, mainBox.x + mainBox.w - chipWidth(ctx, label) - 6, y, label, theme.ink3))
      }
    }

    // 3) 事件标记：锚在具体某根 K 线上的三角 + 芯片标签。
    for (const event of events) {
      const x = timeToX(event.date)
      const y = priceToY(event.price)
      if (x == null || y == null) continue
      if (x < mainBox.x - 20 || x > mainBox.x + mainBox.w + 20) continue
      const style = KIND_STYLE[event.kind] || { role: 'accent', key: null }
      const color = colorOf(theme, style.role)
      const below = event.kind === 'spring' || event.kind === 'lps'
      drawTriangle(ctx, x, y, color, below)
      const label = style.key ? t(style.key) : event.kind
      chips.push(chipAt(ctx, x - chipWidth(ctx, label) / 2, below ? y + 12 : y - CHIP_H - 12, label, color))
    }

    // 先解除裁剪：线条要裁在主图内，但标签贴边时不该被切掉半个。
    ctx.restore()
    ctx.save()
    ctx.font = FONT
    // 4) agent 画的标注。和自动识别的部分同层，但用强调色区分来源。
    //
    // 形状本身必须裁在主图内：price_line 只校验价格能否换算成 y，不管这个 y
    // 有没有落在可见区间里，超出时那条虚线会横穿成交量副图和日期轴。趋势线
    // 现在也用屏外的真实坐标画（保持角度），同样依赖这个裁剪。
    // 芯片不在这层 —— 它们收进 chips，等裁剪解除后再统一画。
    ctx.save()
    ctx.beginPath()
    ctx.rect(view.mainBox.x, view.mainBox.y, view.mainBox.w, view.mainBox.h)
    ctx.clip()
    for (const item of drawn) {
      drawAgentShape(ctx, item, view, theme, chips)
    }
    ctx.restore()

    // 标签最后画，且统一走一次防重叠 —— 这样芯片总在线条之上，也不会互相压。
    for (const chip of layoutChips(clampChips(chips, width), view.height)) drawChip(ctx, chip)
    ctx.restore()
  }
}

/**
 * 画一条 agent 标注。类型未知时静默跳过 —— 后端以后加新 type，旧前端不该报错。
 * 颜色优先用标注自带的 color，否则用强调色。
 */
function drawAgentShape (ctx, item, view, theme, chips) {
  const { mainBox, priceToY } = view
  const color = item.color || theme.accent
  const label = item.label || item.text || ''
  const right = mainBox.x + mainBox.w
  // locateTime 会说明「没有坐标」是哪一种：不在数据集里 / 在可见区间左边 / 右边。
  // 老的 timeToX 把三者压成同一个 null，下面的夹边逻辑就只能靠猜。
  const locate = view.locateTime || ((date) => {
    const x = view.timeToX(date)
    return { x, side: x == null ? 'unknown' : 'in' }
  })

  if (item.type === 'rectangle') {
    const a = locate(item.start_date)
    const b = locate(item.end_date)
    const yHi = priceToY(item.high)
    const yLo = priceToY(item.low)
    if (yHi == null || yLo == null) return
    // 日期压根不在数据里就别猜位置 —— 画出来的是一块凭空的矩形。
    if (a.side === 'unknown' || b.side === 'unknown') return
    // 两端都在同一侧屏外 ⇒ 整块不可见。
    //
    // 这是幻影矩形的根源：以前 start 为 null 就当「出左边」夹到左边界、end 为
    // null 就当「出右边」夹到右边界，于是两端都在左边时反而被夹成横贯全图。
    // 默认视窗 120 根、数据 320 根，用户滚离标注区必然触发。
    if (a.side === b.side && a.side !== 'in') return
    const left = a.side === 'in' ? a.x : mainBox.x
    const rightEdge = b.side === 'in' ? b.x : right
    if (rightEdge <= mainBox.x || left >= right) return
    ctx.fillStyle = withAlpha(color, 0.1)
    ctx.fillRect(left, yHi, Math.max(1, rightEdge - left), Math.max(1, yLo - yHi))
    ctx.strokeStyle = withAlpha(color, 0.7)
    ctx.lineWidth = 1
    ctx.strokeRect(left, yHi, Math.max(1, rightEdge - left), Math.max(1, yLo - yHi))
    if (label) chips.push(chipAt(ctx, left + 4, yHi, label, color))
    return
  }

  if (item.type === 'price_line') {
    const y = priceToY(item.price)
    if (y == null) return
    ctx.strokeStyle = withAlpha(color, 0.8)
    ctx.setLineDash([5, 3])
    ctx.lineWidth = 1
    ctx.beginPath()
    ctx.moveTo(mainBox.x, Math.round(y) + 0.5)
    ctx.lineTo(right, Math.round(y) + 0.5)
    ctx.stroke()
    ctx.setLineDash([])
    if (label) chips.push(chipAt(ctx, mainBox.x + 6, y, label, color))
    return
  }

  if (item.type === 'trendline') {
    const a = locate(item.start_date)
    const b = locate(item.end_date)
    const y1 = priceToY(item.start_price)
    const y2 = priceToY(item.end_price)
    if (y1 == null || y2 == null) return
    if (a.side === 'unknown' || b.side === 'unknown') return
    if (a.side === b.side && a.side !== 'in') return
    // 与矩形相反的毛病：以前任一端出屏，整条线就消失 —— 而趋势线的用途恰恰是
    // 延伸到屏外。用端点的**真实**坐标（可以是负数或超出右边界）画，画布自己会
    // 裁剪，线的角度保持正确。把出屏端夹到边界反而会把线掰弯。
    const x1 = a.projectedX
    const x2 = b.projectedX
    if (x1 == null || x2 == null) return
    ctx.strokeStyle = withAlpha(color, 0.85)
    ctx.lineWidth = 1.5
    ctx.beginPath()
    ctx.moveTo(x1, y1)
    ctx.lineTo(x2, y2)
    ctx.stroke()
    if (label) chips.push(chipAt(ctx, x2 - chipWidth(ctx, label), y2, label, color))
    return
  }

  if (item.type === 'marker' || item.type === 'text') {
    const x = timeToX(item.date)
    const y = priceToY(item.price)
    if (x == null || y == null) return
    if (item.type === 'marker') drawTriangle(ctx, x, y, color, true)
    if (label) chips.push(chipAt(ctx, x - chipWidth(ctx, label) / 2, y + 14, label, color))
  }
}

function chipAt (ctx, left, top, text, color) {
  return { left, top: top - CHIP_H / 2, width: chipWidth(ctx, text), text, color }
}

const clampChips = (chips, width) =>
  chips.map((chip) => ({
    ...chip,
    left: Math.max(EDGE, Math.min(width - chip.width - EDGE, chip.left))
  }))

/** spring/LPS 画在下方朝上，SOS/派发画在上方朝下。 */
function drawTriangle (ctx, x, y, color, below) {
  const size = 5
  const tip = below ? y + 3 : y - 3
  ctx.beginPath()
  if (below) {
    ctx.moveTo(x, tip)
    ctx.lineTo(x - size, tip + size * 1.5)
    ctx.lineTo(x + size, tip + size * 1.5)
  } else {
    ctx.moveTo(x, tip)
    ctx.lineTo(x - size, tip - size * 1.5)
    ctx.lineTo(x + size, tip - size * 1.5)
  }
  ctx.closePath()
  ctx.fillStyle = color
  ctx.fill()
}

/** hex / rgb 字面色 → rgba。canvas 需要通道值，拿不到 CSS 的 color-mix。 */
function withAlpha (color, alpha) {
  const value = String(color || '').trim()
  if (value.startsWith('#')) {
    const hex = value.slice(1)
    const full = hex.length === 3 ? hex.split('').map((c) => c + c).join('') : hex
    const num = parseInt(full, 16)
    if (Number.isNaN(num)) return `rgba(153,153,153,${alpha})`
    return `rgba(${(num >> 16) & 255},${(num >> 8) & 255},${num & 255},${alpha})`
  }
  const nums = value.match(/[\d.]+/g)
  if (nums && nums.length >= 3) return `rgba(${nums[0]},${nums[1]},${nums[2]},${alpha})`
  return `rgba(153,153,153,${alpha})`
}

window.WyckoffAnnotations = {
  createAnnotationPainter,
  layoutChips,
  withAlpha,
  measure,
  KIND_STYLE
}
})()
