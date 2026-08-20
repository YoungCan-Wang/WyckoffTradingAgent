'use strict'

// 清空现金输入框再点别处，不该把现金写成 ¥0。
//
// Number('') === 0，会通过「有限且 >= 0」的检查然后直接保存。而这一行是失焦即存
// 的，所以「全选删除准备重输 → 点了别处」这个完全正常的操作就清空了账户现金。
// 空输入的语义是「我还没填」，不是「零」—— 想记 0 的人会真的打一个 0。
//
// 持仓行因为 shares <= 0 / cost <= 0 被拦住，止损把空串当成显式清除，
// 唯独现金允许 0，于是漏在这里。
//
// 这里把提交逻辑从组件源码里抽出来跑，不需要 React 运行时。
const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')

const SRC = readFileSync(
  join(__dirname, '..', 'src', 'renderer', 'components', 'PortfolioPage.tsx'),
  'utf8'
)

/**
 * 复刻 CashRow.commit 的判定，直接读源码里的那段以免测试与实现分叉。
 *
 * 抽的是「给定当前值和草稿，会不会保存、保存什么」这个纯函数部分。
 */
function makeCommit () {
  const body = SRC.match(/const commit = async \(\) => \{[\s\S]*?\n  \}/)
  assert.ok(body, '找不到 CashRow 的 commit')
  const source = body[0]
    .replace('const commit = async () => {', 'return async function commit () {')
    .replace(/\n  \}$/, '\n  }')

  return (value, draft) => {
    const saved = []
    let errored = null
    let reset = null
    const fn = new Function('draft', 'value', 'setDraft', 'onError', 'onSave', 't', `${source}\n`)(
      draft,
      value,
      (v) => { reset = v },
      (m) => { errored = m },
      async (v) => { saved.push(v) },
      (k) => k
    )
    return fn().then(() => ({ saved, errored, reset }))
  }
}

test('清空后失焦不保存，并把原值填回去', async () => {
  const commit = makeCommit()
  const r = await commit(50000, '')
  assert.deepEqual(r.saved, [], '空输入被当成 0 保存了 —— 用户的现金被清空')
  assert.equal(r.reset, '50000', '应把原值填回输入框')
  assert.equal(r.errored, null, '这不是错误，用户只是没填完；不该弹错误提示')
})

test('只有空白字符也一样', async () => {
  const commit = makeCommit()
  const r = await commit(50000, '   ')
  assert.deepEqual(r.saved, [])
})

test('真的想记 0 就打一个 0，仍然能保存', async () => {
  const commit = makeCommit()
  const r = await commit(50000, '0')
  assert.deepEqual(r.saved, [0], '满仓（现金 0）是合法状态，不能一并拦掉')
})

test('正常修改照常保存', async () => {
  const commit = makeCommit()
  const r = await commit(50000, '80000')
  assert.deepEqual(r.saved, [80000])
})

test('负数报错并回填', async () => {
  const commit = makeCommit()
  const r = await commit(50000, '-1')
  assert.deepEqual(r.saved, [])
  assert.equal(r.errored, 'portfolio.badCash')
  assert.equal(r.reset, '50000')
})

test('非数字报错并回填', async () => {
  const commit = makeCommit()
  const r = await commit(50000, 'abc')
  assert.deepEqual(r.saved, [])
  assert.equal(r.errored, 'portfolio.badCash')
})

test('值没变就不发请求', async () => {
  const commit = makeCommit()
  const r = await commit(50000, '50000')
  assert.deepEqual(r.saved, [])
})

test('原值本来是 0 时，清空同样不触发保存', async () => {
  // 这种情况下即使写成 0 也「没有后果」，但行为要一致 —— 靠「值没变」兜住
  // 是巧合，不是防护。
  const commit = makeCommit()
  const r = await commit(0, '')
  assert.deepEqual(r.saved, [])
})
