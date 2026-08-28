'use strict'

// 确认结果不能报告一件没发生的事。
//
// 就地确认卡有三条失败路径,长得很像但含义完全不同:
//   1. collect 抛错          → 这次点击没送到,可以再点
//   2. collect 返回 null      → 同上。注意它是**返回 null 而不是抛错**,
//                              当成「送到了」就会显示一个假的「已同意」
//   3. delivered=false       → 送到了,但那一轮已经不在等答复(超时收尾,
//                              或这张卡被点过第二次)。操作**没有执行**
// 前两条要留着按钮(retryable),第三条不能 —— 决定权已经不在这张卡手上了。
//
// 原来这里测的是审批页 reload 会不会把结果文案冲掉。那一页没有了:确认在对话
// 里当场问,不再有「决策后离开待批列表」这回事。留下来的是同一个诚实性要求。
const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')

const read = (name) =>
  readFileSync(join(__dirname, '..', 'src', 'renderer', 'components', name), 'utf8')

const CARDS = [['ConfirmCardInline.tsx', read('ConfirmCardInline.tsx')],
  ['QuestionCardInline.tsx', read('QuestionCardInline.tsx')]]

for (const [name, src] of CARDS) {
  test(`${name}: collect 返回 null 不能当成送达`, () => {
    assert.match(src, /if \(!res\)/, 'collect 失败时返回 null,漏判会报告一个假的决定')
    assert.match(src, /retryable: true/, '没送到的点击要留着按钮,否则只能重开一轮')
  })

  test(`${name}: delivered=false 要说实话`, () => {
    assert.match(src, /delivered\?: boolean/, '没有读 delivered,无法知道那一轮还在不在等')
    assert.match(src, /confirm\.expired/, '过期时缺少「没有送达」的文案')
  })

  test(`${name}: 只有可重试的失败才留按钮`, () => {
    assert.match(
      src,
      /!outcome \|\| outcome\.retryable/,
      '已经作过决定还留着按钮,会让人以为可以改;反之点击没送到却收走按钮,就没法再试'
    )
  })
}

test('ConfirmCardInline: 拒绝不触发缓存作废', () => {
  const src = read('ConfirmCardInline.tsx')
  // 拒绝意味着没有执行,数据没变。无条件 invalidate 会重拉一遍相同的数据,
  // 更糟的是让人以为有什么被改动了。
  assert.match(src, /if \(approved\) onDecided/, '拒绝后不该作废缓存 —— 什么都没执行')
})
