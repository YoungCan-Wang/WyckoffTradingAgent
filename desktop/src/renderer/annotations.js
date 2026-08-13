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
    top = Math.max(EDGE, Math.min(height - CHIP_H - EDGE, top))
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
    // 标签最后画，且统一走一次防重叠 —— 这样芯片总在线条之上，也不会互相压。
    for (const chip of layoutChips(clampChips(chips, width), view.height)) drawChip(ctx, chip)
    ctx.restore()
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
