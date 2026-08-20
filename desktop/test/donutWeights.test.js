'use strict'

// 持仓集中度的百分比必须相对**全部**持仓算，不是相对「前 5 名之和」。
//
// 原来 deriveSectors 先 slice(0,5) 再由 sectorDonut 对这 5 个求和当分母：
// 10 只等权持仓每只显示 20%，真实占比是 10%。在一个反复强调「不发明数字」的
// 文件里，集中度这个数字本身是错的 —— 而且是往高报，正好会让人以为集中度
// 风险比实际更大。
const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')

const SRC = readFileSync(join(__dirname, '..', 'src', 'renderer', 'charts.js'), 'utf8')

/** 从 charts.js 里取出 deriveSectors 与它依赖的常量，单独执行。 */
function loadDeriveSectors () {
  const fn = SRC.match(/function deriveSectors[\s\S]*?\n}/)
  const topBuckets = SRC.match(/const TOP_BUCKETS = \d+/)
  assert.ok(fn, '找不到 deriveSectors')
  assert.ok(topBuckets, '找不到 TOP_BUCKETS —— 截断上限应该是个具名常量')
  const body = `${topBuckets[0]}\n${fn[0]}\nreturn deriveSectors`
  return new Function('t', body)((key) => key)
}

const position = (code, value) => ({ code, name: code, shares: 1, cost_price: value })

test('10 只等权持仓，每只显示真实的 10%', () => {
  const deriveSectors = loadDeriveSectors()
  const positions = Array.from({ length: 10 }, (_, i) => position(`C${i}`, 100))
  const g = deriveSectors(positions)

  assert.equal(g.total, 1000, 'total 必须是全部持仓的合计')
  for (const item of g.items) {
    const pct = (item.weight / g.total) * 100
    assert.ok(Math.abs(pct - 10) < 1e-9, `应为 10%，实际 ${pct.toFixed(1)}%（旧代码给 20%）`)
  }
})

test('被截断时给出标记与真实合计', () => {
  const deriveSectors = loadDeriveSectors()
  const g = deriveSectors(Array.from({ length: 8 }, (_, i) => position(`C${i}`, 50)))
  assert.equal(g.truncated, true)
  assert.equal(g.items.length, 5, '只展示前 5 个桶')
  assert.equal(g.total, 400, 'total 仍是 8 只的合计')
  const shown = g.items.reduce((sum, i) => sum + i.weight, 0)
  assert.ok(shown < g.total, '前 5 名之和必须小于总额，否则「其他」那段就凭空来的')
})

test('不超过 5 个桶时不标记截断，占比合计为 100%', () => {
  const deriveSectors = loadDeriveSectors()
  const g = deriveSectors([position('A', 60), position('B', 40)])
  assert.equal(g.truncated, false)
  const pct = g.items.reduce((sum, i) => sum + (i.weight / g.total) * 100, 0)
  assert.ok(Math.abs(pct - 100) < 1e-9, `应合计 100%，实际 ${pct}`)
})

test('权重从大到小排序', () => {
  const deriveSectors = loadDeriveSectors()
  const g = deriveSectors([position('A', 10), position('B', 90), position('C', 50)])
  assert.deepEqual(g.items.map((i) => i.name), ['B', 'C', 'A'])
})

test('按行业分组时同理', () => {
  const deriveSectors = loadDeriveSectors()
  const positions = [
    { code: 'A', name: 'A', sector: '白酒', shares: 1, cost_price: 100 },
    { code: 'B', name: 'B', sector: '白酒', shares: 1, cost_price: 100 },
    { code: 'C', name: 'C', sector: '银行', shares: 1, cost_price: 200 }
  ]
  const g = deriveSectors(positions)
  assert.equal(g.hasSector, true)
  assert.equal(g.total, 400)
  const bank = g.items.find((i) => i.name === '银行')
  assert.ok(Math.abs((bank.weight / g.total) * 100 - 50) < 1e-9)
})

test('空持仓不炸', () => {
  const deriveSectors = loadDeriveSectors()
  const g = deriveSectors([])
  assert.deepEqual(g.items, [])
  assert.equal(g.total, 0)
})
