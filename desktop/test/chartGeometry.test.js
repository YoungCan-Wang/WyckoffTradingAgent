'use strict'

// 图表坐标契约与标注绘制的边界情况。
//
// 这些都不是「看起来不好看」，而是画出**不存在的东西**：
//
// 1. timeToX 把三件事压成同一个 null（日期不在数据里 / K 线在可见区间左边 /
//    右边）。矩形绘制假设「start 为 null 就是出左边、end 为 null 就是出右边」，
//    于是两端都在左侧屏外时反而被夹成横贯全图的幻影矩形。默认视窗 120 根、
//    数据 320 根，用户滚离标注区必然触发。
// 2. 趋势线是反向毛病：任一端出屏整条线消失，而趋势线的用途恰恰是延伸到屏外。
// 3. 可见区间价格全相等（一字板）时 priceSpan 返回 {lo:0,hi:1}，丢掉真实价位，
//    整屏 K 线被画到画布外，看起来像没数据。
const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')

const R = (...p) => join(__dirname, '..', 'src', 'renderer', ...p)

/** 在一个最小 DOM 替身里加载 kline.js，取出 createKlineChart。 */
function loadKline () {
  const stubNode = () => {
    const node = {
      className: '',
      style: { setProperty () {} },
      classList: { add () {}, remove () {}, toggle () {}, contains: () => false },
      dataset: {},
      children: [],
      appendChild (c) { this.children.push(c); return c },
      append (...c) { this.children.push(...c) },
      replaceChildren (...c) { this.children = c },
      setAttribute () {},
      addEventListener () {},
      removeEventListener () {},
      getBoundingClientRect: () => ({ width: 800, height: 400, left: 0, top: 0 }),
      getContext: () => stubCtx()
    }
    return node
  }
  const stubCtx = () => new Proxy({
    canvas: { width: 800, height: 400 },
    measureText: () => ({ width: 20 }),
    setLineDash () {},
    createLinearGradient: () => ({ addColorStop () {} })
  }, {
    get: (target, key) => (key in target ? target[key] : () => {}),
    set: () => true
  })

  const win = {
    devicePixelRatio: 1,
    matchMedia: () => ({ matches: false, addEventListener () {}, removeEventListener () {} }),
    addEventListener () {},
    removeEventListener () {},
    requestAnimationFrame: (fn) => { fn(); return 1 },
    cancelAnimationFrame () {},
    ResizeObserver: class { observe () {} disconnect () {} },
    getComputedStyle: () => ({ getPropertyValue: () => '' })
  }
  const doc = {
    documentElement: { classList: { contains: () => false } },
    createElement: stubNode,
    createElementNS: stubNode
  }
  win.document = doc

  const src = readFileSync(R('kline.js'), 'utf8')
  new Function('window', 'document', 'getComputedStyle', 'ResizeObserver', src)(
    win, doc, win.getComputedStyle, win.ResizeObserver
  )
  return win.WyckoffKline
}

/** 造 n 根 K 线；priceAt 可自定义每根的价格。 */
function makeBars (n, priceAt = () => 10) {
  const bars = { date: [], open: [], high: [], low: [], close: [], volume: [] }
  for (let i = 0; i < n; i += 1) {
    const p = priceAt(i)
    // 用 2026-01-01 起的连续编号，日期本身只要唯一即可
    bars.date.push(`2026-${String(Math.floor(i / 28) + 1).padStart(2, '0')}-${String((i % 28) + 1).padStart(2, '0')}`)
    bars.open.push(p)
    bars.high.push(p)
    bars.low.push(p)
    bars.close.push(p)
    bars.volume.push(1000)
  }
  return bars
}

test('locateTime 区分「不在数据里」「出左边」「出右边」', () => {
  const kline = loadKline()
  const bars = makeBars(320, (i) => 10 + i * 0.1)
  const chart = kline.createKlineChart({ bars })
  const { from, to } = chart.visibleRange()

  // 默认视窗是最后 120 根，所以前面那些都在左边
  const leftDate = bars.date[0]
  const inDate = bars.date[from + 5]
  const rightSide = to < bars.date.length ? bars.date[to] : null

  assert.equal(chart.locateTime(leftDate).side, 'left', '早于可见区间应报 left')
  assert.equal(chart.locateTime(inDate).side, 'in')
  assert.equal(chart.locateTime('1999-01-01').side, 'unknown', '不在数据集里应报 unknown')
  if (rightSide) assert.equal(chart.locateTime(rightSide).side, 'right')

  // 老的 timeToX 对前三种里的两种都只给 null —— 这就是幻影矩形的根源
  assert.equal(chart.timeToX(leftDate), null)
  assert.equal(chart.timeToX('1999-01-01'), null)
})

test('locateTime 对屏外端点仍给出真实坐标，趋势线才能保持角度', () => {
  const kline = loadKline()
  const bars = makeBars(320, (i) => 10 + i * 0.1)
  const chart = kline.createKlineChart({ bars })
  const left = chart.locateTime(bars.date[0])
  assert.equal(left.x, null, 'x 仍然是 null —— 表示「不在可见区间」')
  assert.equal(typeof left.projectedX, 'number', 'projectedX 要有值')
  assert.ok(left.projectedX < 0, `屏左的端点坐标应为负，实际 ${left.projectedX}`)
})

test('一字板（可见区间价格全相等）不会把 K 线画到画布外', () => {
  const kline = loadKline()
  // 全部 10 元，hi === lo
  const chart = kline.createKlineChart({ bars: makeBars(60, () => 10) })
  const y = chart.priceToY(10)
  assert.ok(Number.isFinite(y), 'priceToY 应给出有限值')
  // 原来返回 {lo:0,hi:1}，priceToY(10) = m.y + (1-10)*m.h，远在画布上方
  assert.ok(y > 0 && y < 400, `一字板的 y 应落在画布内，实际 ${y}`)
})

test('一字板时价格轴仍围绕真实价位，而不是 0~1', () => {
  const kline = loadKline()
  const chart = kline.createKlineChart({ bars: makeBars(60, () => 1500) })
  const y = chart.priceToY(1500)
  assert.ok(Number.isFinite(y) && y > 0 && y < 400, `高价一字板也要在画布内，实际 ${y}`)
  // 真实价位附近应该单调：略高的价格 y 更小（画得更靠上）
  assert.ok(chart.priceToY(1501) < y, '价格轴方向反了')
})

test('价格格式化对 null / NaN 不抛异常', () => {
  // _columnar 会把 NaN 换成 None -> JSON null（停牌、缺列都会产生）。
  // Math.abs(null) === 0 会绕过 >= 100 的阈值判断，直接走到 null.toFixed()。
  // 画蜡烛的循环有判空，读数条（syncReadout）没有 —— 悬停到这样的 K 线时
  // mousemove 会持续抛 TypeError。
  const src = readFileSync(R('kline.js'), 'utf8')
  const line = src.match(/const fmtPrice = .*/)
  assert.ok(line, '找不到 fmtPrice')
  const fmtPrice = new Function(`${line[0]}; return fmtPrice`)()

  for (const bad of [null, undefined, NaN, Infinity]) {
    assert.doesNotThrow(() => fmtPrice(bad), `fmtPrice(${bad}) 抛异常了`)
    assert.equal(typeof fmtPrice(bad), 'string', '缺值也要返回字符串')
  }
  // 正常值的格式不能被改坏：高价一位小数、低价两位
  assert.equal(fmtPrice(1500), '1500.0')
  assert.equal(fmtPrice(12.345), '12.35')
})

test('悬停到缺值 K 线时读数条不炸', () => {
  const kline = loadKline()
  // 中间那根全是 null —— 停牌日的真实形状
  const bars = makeBars(40, (i) => 10 + i * 0.1)
  for (const key of ['open', 'high', 'low', 'close']) bars[key][20] = null
  const chart = kline.createKlineChart({ bars })
  // priceToY 对 null 返回 null（本来就有防护），这里验的是格式化那条路径
  assert.equal(chart.priceToY(bars.close[20]), null)
  assert.doesNotThrow(() => chart.draw(), '含缺值的 K 线绘制不该抛异常')
})
