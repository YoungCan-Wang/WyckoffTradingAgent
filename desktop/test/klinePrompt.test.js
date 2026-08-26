'use strict'

// 「打开 > K 线图」曾经完全没反应，两个独立的 bug 叠在一起：
//
// 1. openSymBox 用 `openBtn.getBoundingClientRect()` 定位，而 openBtn 从来没有
//    定义过 —— 顶栏那个「打开」按钮现在是 React 组件。抛 ReferenceError，
//    函数在建好浮层之前就死了。
// 2. 修掉之后仍然没反应：那个「点外面关掉」的全局 click 监听是在同一轮事件里
//    注册的，而打开浮层的那次 click 还在冒泡 —— 浮层刚 append 就被自己关掉。
//
// 两者的表现完全一样（什么都不出现），所以修完第一个我以为搞定了，
// 探针才告诉我 symbox 依然不存在。
const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')

const SRC = readFileSync(join(__dirname, '..', 'src', 'renderer', 'shell.js'), 'utf8')

test('openSymBox 不引用未定义的 openBtn', () => {
  assert.ok(!/\bopenBtn\b/.test(SRC), 'openBtn 已不存在于 DOM（那个按钮归 React），引用它必抛 ReferenceError')
})

test('openSymBox 从参数拿锚点', () => {
  assert.match(SRC, /function openSymBox \(anchor\)/, '锚点必须由调用方传入')
  assert.match(SRC, /anchor && anchor\.getBoundingClientRect/, '拿不到锚点时要有兜底，不能抛错')
})

test('关闭浮层的监听延到下一帧注册', () => {
  // 同一轮注册 = 立刻收到那次仍在冒泡的 click = 浮层自己把自己关掉
  assert.match(
    SRC,
    /requestAnimationFrame\(\(\) => window\.addEventListener\('click', onDocClick\)\)/,
    '必须延后注册，否则打开浮层的那次 click 会立刻关掉它'
  )
  assert.match(SRC, /armSymBoxDismiss\(\)/, 'openSymBox 结尾要装载关闭监听')
})

test('浮层内部的点击不关闭', () => {
  assert.match(SRC, /closest\('\.symbox'\)\) return/, '点输入框/按钮不该关掉浮层')
})
