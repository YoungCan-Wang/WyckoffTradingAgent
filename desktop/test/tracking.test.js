'use strict'

// 跟踪表的去重与排序。空值排序方向是最容易写错的地方：只要把 direction
// 乘到「有一边为空」的分支上，升序时就会一屏全是没数据的行。
const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const ts = require('typescript')

// 直接编译 TS 源来测，避免维护一份 JS 副本。
const SRC = join(__dirname, '..', 'src', 'renderer', 'lib', 'tracking.ts')
const js = ts.transpileModule(readFileSync(SRC, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 }
}).outputText
const mod = { exports: {} }
new Function('module', 'exports', 'require', js)(mod, mod.exports, require)
const { dedupeByCode, sortRows, filterRows, displayCode } = mod.exports

const row = (over) => ({
  code: '600519', name: '贵州茅台', recommend_date: '20260818',
  recommend_price: 100, current_price: 110, pnl_pct: 10,
  max_pnl_pct: 12, min_pnl_pct: -2, camp: '', status: '跟踪中',
  is_ai_recommended: false, entry_role: '观察/信号复盘', ...over
})

test('去重：同一代码只留最新推荐日', () => {
  const out = dedupeByCode([
    row({ code: 'A', recommend_date: '20260810', current_price: 10 }),
    row({ code: 'A', recommend_date: '20260818', current_price: 20 }),
    row({ code: 'B', recommend_date: '20260812' })
  ])
  assert.equal(out.length, 2)
  const a = out.find((r) => r.code === 'A')
  assert.equal(a.recommend_date, '20260818')
  assert.equal(a.current_price, 20)
})

test('去重：推荐价沿用最早那条', () => {
  // 用最新一条的初始价算涨跌会把已经走过的一段抹掉。
  const out = dedupeByCode([
    row({ code: 'A', recommend_date: '20260801', recommend_price: 50 }),
    row({ code: 'A', recommend_date: '20260818', recommend_price: 80 })
  ])
  assert.equal(out[0].recommend_price, 50)
})

test('去重：忽略空代码', () => {
  assert.equal(dedupeByCode([row({ code: '' }), row({ code: 'A' })]).length, 1)
})

test('排序：降序时数值从大到小', () => {
  const out = sortRows([row({ code: 'A', pnl_pct: 1 }), row({ code: 'B', pnl_pct: 9 })], 'change', 'desc')
  assert.deepEqual(out.map((r) => r.pnl_pct), [9, 1])
})

test('排序：升序时数值从小到大', () => {
  const out = sortRows([row({ code: 'A', pnl_pct: 9 }), row({ code: 'B', pnl_pct: 1 })], 'change', 'asc')
  assert.deepEqual(out.map((r) => r.pnl_pct), [1, 9])
})

test('排序：空值在降序和升序里都沉底', () => {
  const rows = [
    row({ code: 'A', pnl_pct: null }),
    row({ code: 'B', pnl_pct: 5 }),
    row({ code: 'C', pnl_pct: null }),
    row({ code: 'D', pnl_pct: -3 })
  ]
  for (const dir of ['desc', 'asc']) {
    const out = sortRows(rows, 'change', dir)
    const nulls = out.map((r) => r.pnl_pct === null)
    // 前两个必须有值，后两个必须是空 —— 这是这组测试的全部意义
    assert.deepEqual(nulls, [false, false, true, true], `${dir} 方向空值没沉底`)
  }
})

test('排序：同一天按代码兜底，顺序稳定', () => {
  const rows = [row({ code: 'C' }), row({ code: 'A' }), row({ code: 'B' })]
  const once = sortRows(rows, 'date', 'desc').map((r) => r.code)
  const twice = sortRows(rows, 'date', 'desc').map((r) => r.code)
  assert.deepEqual(once, ['A', 'B', 'C'])
  assert.deepEqual(once, twice)
})

test('排序：不修改传入的数组', () => {
  const rows = [row({ code: 'B', pnl_pct: 1 }), row({ code: 'A', pnl_pct: 9 })]
  sortRows(rows, 'change', 'desc')
  assert.equal(rows[0].code, 'B', '就地排序会让 React 收不到变更')
})

test('筛选：小写关键词能搜到大写美股代码', () => {
  // web 端这里有 bug（只把关键词转小写、没转代码），不要跟着错。
  const rows = [row({ code: 'AAPL.US', name: '苹果' }), row({ code: '600519' })]
  assert.equal(filterRows(rows, { query: 'aapl', aiOnly: false, days: 0 }).length, 1)
})

test('筛选：按名称也能搜', () => {
  const rows = [row({ code: 'A', name: '贵州茅台' }), row({ code: 'B', name: '平安银行' })]
  assert.equal(filterRows(rows, { query: '茅台', aiOnly: false, days: 0 }).length, 1)
})

test('筛选：只看 AI', () => {
  const rows = [row({ code: 'A', is_ai_recommended: true }), row({ code: 'B' })]
  assert.equal(filterRows(rows, { query: '', aiOnly: true, days: 0 }).length, 1)
})

test('筛选：日期窗口按推荐日个数而非日历天数', () => {
  const rows = [
    row({ code: 'A', recommend_date: '20260818' }),
    row({ code: 'B', recommend_date: '20260815' }),
    row({ code: 'C', recommend_date: '20260801' })
  ]
  const out = filterRows(rows, { query: '', aiOnly: false, days: 2 })
  assert.deepEqual(out.map((r) => r.code), ['A', 'B'])
})

test('筛选：days=0 表示不限', () => {
  const rows = [row({ code: 'A', recommend_date: '20260818' }), row({ code: 'B', recommend_date: '20200101' })]
  assert.equal(filterRows(rows, { query: '', aiOnly: false, days: 0 }).length, 2)
})

test('代码显示：A 股补零，其他市场原样', () => {
  assert.equal(displayCode('998', 'cn'), '000998')
  assert.equal(displayCode('600519', 'cn'), '600519')
  // 港股 5 位补零会变成错的代码，所以只对 A 股补。
  assert.equal(displayCode('00700.HK', 'hk'), '00700.HK')
  assert.equal(displayCode('AAPL.US', 'us'), 'AAPL.US')
})
