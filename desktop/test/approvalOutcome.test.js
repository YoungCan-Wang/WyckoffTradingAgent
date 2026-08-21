'use strict'

// 决策结果不能在 reload 里被冲掉。
//
// decide() 之后要 reload 才能刷掉已决策的项,而 useIpc 的 reload 会把 loading
// 打回 true。审批页顶上那句 `if (loading) return <加载中>` 会把整个列表连同刚
// 写好的「执行失败」文案一起卸载 —— 用户批准一笔失败的操作,只看到列表少一项,
// **看不到失败信息**。而这一页自己的注释写着「执行了但失败要明说失败」。
//
// 与 SchedulesPage 的区别:那边的项重跑后仍在列表里,页面级 notes 就够;
// 审批项一旦决策就离开待批列表,所以还需要一条独立的结果横幅。
const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')

const SRC = readFileSync(join(__dirname, '..', 'src', 'renderer', 'components', 'ApprovalsPage.tsx'), 'utf8')

test('首次加载才整页显示「加载中」', () => {
  assert.match(
    SRC,
    /if \(loading && !data\) return/,
    '无条件的 if (loading) 会在每次 reload 时卸载整个列表（连同结果文案）'
  )
})

test('已决策但已离开待批列表的结果仍要渲染', () => {
  assert.match(SRC, /const pendingIds = new Set/, '要能区分「还在待批」和「已决策」')
  assert.match(SRC, /const decided = Object\.entries\(outcome\)/, '缺少已决策项的结果集合')
  // 可重试的（调用没走通）不算已决策 —— 那些项还在列表里,按钮也还留着
  assert.match(SRC, /!out\.retryable/, '「调用失败可重试」不该被当成已决策结果')
  assert.match(SRC, /decided\.map/, '结果集合没有被渲染出来')
})

test('空列表判断要把已决策结果算进去', () => {
  // 否则决策掉最后一项时,会立刻显示「没有待批」而把结果文案挤掉
  assert.match(
    SRC,
    /if \(!items\.length && !decided\.length\)/,
    '决策掉最后一项后,结果文案会被「没有待批」覆盖'
  )
})
