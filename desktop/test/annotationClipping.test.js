'use strict'

// 标注在屏外时不该画出不存在的东西。
//
// 幻影矩形：timeToX 对「日期不在数据里」「K 线在可见区间左边」「在右边」都返回
// null，绘制代码却假设 start 为 null 就是出左边、end 为 null 就是出右边，于是
// **两端都在左侧屏外**时被夹成横贯全图的矩形。默认视窗 120 根、数据 320 根，
// 用户滚离标注区必然触发。
//
// 这里用一个记录调用的假 ctx 直接看「画了什么」，而不是断言源码文本。
const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')

const R = (...p) => join(__dirname, '..', 'src', 'renderer', ...p)

/** 记录所有 fillRect / strokeRect / moveTo / lineTo 的假 ctx。 */
function recordingCtx () {
  const calls = []
  const ctx = new Proxy({
    calls,
    canvas: { width: 800, height: 400 },
    measureText: (s) => ({ width: String(s).length * 6 })
  }, {
    get (target, key) {
      if (key in target) return target[key]
      return (...args) => { calls.push({ op: key, args }) }
    },
    set () { return true }
  })
  return ctx
}

function loadAnnotations () {
  const win = { WyckoffI18n: { t: (k) => k } }
  const doc = { documentElement: { classList: { contains: () => false } } }
  const src = readFileSync(R('annotations.js'), 'utf8')
  new Function('window', 'document', src)(win, doc)
  return win.WyckoffAnnotations
}

const MAIN = { x: 40, y: 10, w: 600, h: 300 }

/**
 * 造一个 view：只有 inDates 里的日期在可见区间内，knownDates 里的在数据集里。
 */
function makeView ({ inDates, knownDates }) {
  const known = new Set(knownDates || inDates)
  const inSet = new Set(inDates)
  const order = [...known]
  return {
    mainBox: MAIN,
    height: 400,
    width: 800,
    priceToY: (p) => (p == null || !isFinite(p) ? null : MAIN.y + MAIN.h / 2 - p),
    timeToX: (d) => (inSet.has(String(d)) ? MAIN.x + 100 : null),
    locateTime: (d) => {
      const key = String(d)
      if (!known.has(key)) return { x: null, projectedX: null, side: 'unknown' }
      if (inSet.has(key)) return { x: MAIN.x + 100, projectedX: MAIN.x + 100, side: 'in' }
      // 约定：排在可见日期之前的算左边，之后的算右边
      const firstIn = order.findIndex((d2) => inSet.has(d2))
      const at = order.indexOf(key)
      return at < firstIn
        ? { x: null, projectedX: MAIN.x - 500, side: 'left' }
        : { x: null, projectedX: MAIN.x + MAIN.w + 500, side: 'right' }
    },
    visibleRange: () => ({ from: 0, to: 1 })
  }
}

/** 跑一遍画笔，返回记录到的矩形。 */
function drawnRects (data, view) {
  const ann = loadAnnotations()
  const ctx = recordingCtx()
  const painter = ann.createAnnotationPainter(data)
  painter({ ctx, width: 800, height: 400, theme: { accent: '#d97757' }, ...view })
  return ctx.calls.filter((c) => c.op === 'fillRect' || c.op === 'strokeRect')
}

test('两端都在左侧屏外时不画矩形（幻影矩形）', () => {
  const view = makeView({ inDates: ['2026-06-01'], knownDates: ['2026-01-01', '2026-01-05', '2026-06-01'] })
  const rects = drawnRects({
    annotations: [{ type: 'rectangle', start_date: '2026-01-01', end_date: '2026-01-05', low: 10, high: 20 }]
  }, view)
  // 老代码会画一个宽度约等于整个主图的矩形
  const wide = rects.filter((r) => r.args[2] > MAIN.w * 0.8)
  assert.equal(wide.length, 0, `画出了横贯全图的幻影矩形: ${JSON.stringify(wide)}`)
})

test('日期完全不在数据集里时不画矩形', () => {
  const view = makeView({ inDates: ['2026-06-01'] })
  const rects = drawnRects({
    annotations: [{ type: 'rectangle', start_date: '1999-01-01', end_date: '1999-02-01', low: 10, high: 20 }]
  }, view)
  assert.equal(rects.length, 0, '凭空画了一块矩形')
})

test('一端在屏内一端在屏外时仍然画，且夹到边界', () => {
  const view = makeView({ inDates: ['2026-06-01'], knownDates: ['2026-01-01', '2026-06-01'] })
  const rects = drawnRects({
    annotations: [{ type: 'rectangle', start_date: '2026-01-01', end_date: '2026-06-01', low: 10, high: 20 }]
  }, view)
  assert.ok(rects.length > 0, '部分可见的矩形不该整块消失')
  for (const r of rects) {
    assert.ok(r.args[0] >= MAIN.x - 1, `左边界越界: ${r.args[0]}`)
  }
})

test('两端都在屏内时正常画', () => {
  const view = makeView({ inDates: ['2026-06-01', '2026-06-10'] })
  const rects = drawnRects({
    annotations: [{ type: 'rectangle', start_date: '2026-06-01', end_date: '2026-06-10', low: 10, high: 20 }]
  }, view)
  assert.ok(rects.length > 0)
})

test('趋势线一端出屏仍然画出来（不再整条消失）', () => {
  const ann = loadAnnotations()
  const ctx = recordingCtx()
  const view = makeView({ inDates: ['2026-06-01'], knownDates: ['2026-01-01', '2026-06-01'] })
  const painter = ann.createAnnotationPainter({
    annotations: [{
      type: 'trendline',
      start_date: '2026-01-01',
      start_price: 10,
      end_date: '2026-06-01',
      end_price: 20
    }]
  })
  painter({ ctx, width: 800, height: 400, theme: { accent: '#d97757' }, ...view })
  const strokes = ctx.calls.filter((c) => c.op === 'lineTo' || c.op === 'moveTo')
  assert.ok(strokes.length > 0, '一端出屏就整条消失 —— 趋势线的用途恰恰是延伸到屏外')
})

test('agent 标注绘制被裁剪在主图内', () => {
  // price_line 只校验价格能不能换算成 y，不管 y 有没有落在可见区间里。
  // 没有裁剪时，超出区间的价格线会横穿成交量副图和日期轴。
  const ann = loadAnnotations()
  const ctx = recordingCtx()
  const view = makeView({ inDates: ['2026-06-01'] })
  const painter = ann.createAnnotationPainter({
    annotations: [{ type: 'price_line', price: -400 }] // 换算后远在主图下方
  })
  painter({ ctx, width: 800, height: 400, theme: { accent: '#d97757' }, ...view })
  const clips = ctx.calls.filter((c) => c.op === 'clip')
  assert.ok(clips.length > 0, '画 agent 标注时必须有裁剪，否则会画到成交量图和日期轴上')
})
