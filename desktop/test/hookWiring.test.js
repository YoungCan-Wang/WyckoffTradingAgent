'use strict'

// 命令式模块（shell.js）与 React（main.tsx）的接线不能依赖执行顺序。
//
// index.html 里 shell.js 是普通脚本、main.tsx 是 type="module"，后者一定后
// 执行。所以在 shell.js 里写 `if (window.WyckoffReact) …` 恒为假 —— 曾经
// 因此让设置页「退出登录」点了没反应，而且完全静默。
//
// 这组测试锁住「谁先就绪都能接上」，并且直接检查 index.html 的脚本类型，
// 免得将来有人把 shell.js 也改成 module 后以为可以省掉这套握手。
//
// （app.js 已拆分：外壳归 React，命令式部分留在 shell.js。）
const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')

const R = (...p) => join(__dirname, '..', 'src', 'renderer', ...p)

test('index.html：shell.js 是普通脚本，main.tsx 是 module', () => {
  const html = readFileSync(R('index.html'), 'utf8')
  const appTag = html.match(/<script[^>]*src="[^"]*shell\.js"[^>]*>/)
  const mainTag = html.match(/<script[^>]*src="[^"]*main\.tsx"[^>]*>/)
  assert.ok(appTag, '找不到 shell.js 的 script 标签')
  assert.ok(mainTag, '找不到 main.tsx 的 script 标签')
  // 这就是整个问题的前提：module 会被延后到文档解析完之后
  assert.ok(!/type=["']module["']/.test(appTag[0]), 'shell.js 变成 module 了，握手假设需要重新评估')
  assert.ok(/type=["']module["']/.test(mainTag[0]), 'main.tsx 应是 module')
})

test('shell.js 只通过 window.WyckoffShell 暴露自己，不反向假设 React 已就绪', () => {
  const src = readFileSync(R('shell.js'), 'utf8')
  // 这个写法恒为假 —— shell.js 一定早于 main.tsx。
  const badGuard = /if\s*\(\s*window\.WyckoffReact\s*\)\s*\{[\s\S]{0,80}setHooks/
  assert.ok(!badGuard.test(src), '又回到了依赖执行顺序的写法')
  assert.ok(src.includes('window.WyckoffShell'), 'shell.js 应把自己挂到 WyckoffShell 供 React 调用')
})

test('main.tsx 挂好 WyckoffReact 之后才去取停放的 hooks', () => {
  const src = readFileSync(R('main.tsx'), 'utf8')
  assert.ok(src.includes('WyckoffPendingHooks'), 'main.tsx 没去取 pending hooks')
  assert.ok(
    src.indexOf('window.WyckoffReact =') < src.indexOf('window.WyckoffPendingHooks'),
    '应先挂好 WyckoffReact 再取 pending hooks'
  )
})

test('模拟真实加载顺序：app.js 先跑，hooks 仍能接上', () => {
  // 只验握手协议本身，不加载真实模块（它们依赖大量 DOM）
  const win = {}
  // 1) app.js 先执行：停放 hooks，此时 WyckoffReact 还不存在
  const hooks = { onSignOut: () => 'signed-out' }
  win.WyckoffPendingHooks = hooks
  if (win.WyckoffReact && win.WyckoffReact.setHooks) win.WyckoffReact.setHooks(hooks)
  assert.equal(win.WyckoffReact, undefined, '前提：此时 React 还没就绪')

  // 2) main.tsx 后执行：挂好自己再取走
  let live = { onSignOut: () => 'default-noop' }
  win.WyckoffReact = { setHooks: (next) => { live = { ...live, ...next } } }
  if (win.WyckoffPendingHooks) win.WyckoffReact.setHooks(win.WyckoffPendingHooks)

  assert.equal(live.onSignOut(), 'signed-out', 'hooks 没接上，退出登录会没反应')
})

test('反向顺序也要成立：React 先就绪', () => {
  const win = {}
  let live = { onSignOut: () => 'default-noop' }
  win.WyckoffReact = { setHooks: (next) => { live = { ...live, ...next } } }
  if (win.WyckoffPendingHooks) win.WyckoffReact.setHooks(win.WyckoffPendingHooks)

  const hooks = { onSignOut: () => 'signed-out' }
  win.WyckoffPendingHooks = hooks
  if (win.WyckoffReact && win.WyckoffReact.setHooks) win.WyckoffReact.setHooks(hooks)

  assert.equal(live.onSignOut(), 'signed-out')
})
