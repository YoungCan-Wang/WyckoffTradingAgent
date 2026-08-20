'use strict'

// 桥回的 id 是**数字**（python-bridge.js 用自增计数器），事件里带的也是数字。
// 我在 useChat 里把它塞进 Set 时没转字符串，分发时却用 String(event.id) 去查
// —— Set.has('9') 对 9 恒为假，于是每一条事件都被丢掉，界面永远停在
// 「正在思考…」。types.ts 把 id 写成 string 撒了谎，所以 tsc 也没报警。
//
// 这组测试锁住「两边都归一成字符串」。
const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')

const R = (...p) => join(__dirname, '..', 'src', 'renderer', ...p)

test('useChat 注册与查找用同一种 id 表示', () => {
  const src = readFileSync(R('lib/useChat.ts'), 'utf8')
  // 注册侧必须显式转字符串
  assert.match(src, /const id = String\(res\.id\)/, '注册 liveIds 时必须 String(res.id)')
  assert.match(src, /liveIds\.current\.add\(id\)/, '应把归一后的 id 加进去')
  // 查找侧本来就是 String(event.id)
  assert.match(src, /const id = String\(event\.id \|\| ''\)/, '分发时应 String(event.id)')
  // 不能再出现直接塞原始值的写法
  assert.ok(!/liveIds\.current\.add\(res\.id\)/.test(src), '又把未转换的 res.id 塞进 Set 了')
})

test('turn 的 id 与 liveIds 用的是同一个值', () => {
  const src = readFileSync(R('lib/useChat.ts'), 'utf8')
  // 曾经是 `id: res.id as string` —— 那个 as 断言正是掩盖类型不符的地方
  assert.ok(!/id: res\.id as string/.test(src), '不要用 as string 掩盖数字 id')
  assert.match(src, /\{ id, user: body/, 'turn 应复用归一后的 id')
})

test('types.ts 不再声称 id 是 string', () => {
  const src = readFileSync(R('types.ts'), 'utf8')
  // 类型撒谎比没有类型更糟：它让这个 bug 通过了编译
  assert.match(src, /id\?: string \| number/, 'PyEvent.id 应允许 number')
  assert.match(src, /id\?: string \| number; error\?: string/, 'call 的返回 id 应允许 number')
})
