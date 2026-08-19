'use strict'

// 评审 P1-2：app.js 与 main.tsx 的接线不能依赖执行顺序。
//
// index.html 里 app.js 是普通脚本、main.tsx 是 type="module"，后者一定后执行。
// 所以 app.js 里写 `if (window.WyckoffReact) setHooks(...)` 恒为假，React 一直
// 用着默认空函数 —— 退出登录点了没反应、模型改了输入区不刷新，而且完全静默。
//
// 这组测试锁住「谁先就绪都能接上」，并且直接检查 index.html 的脚本类型，
// 免得将来有人把 app.js 也改成 module 后以为可以省掉这套握手。
const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')

const R = (...p) => join(__dirname, '..', 'src', 'renderer', ...p)

test('index.html：app.js 是普通脚本，main.tsx 是 module', () => {
  const html = readFileSync(R('index.html'), 'utf8')
  const appTag = html.match(/<script[^>]*src="[^"]*app\.js"[^>]*>/)
  const mainTag = html.match(/<script[^>]*src="[^"]*main\.tsx"[^>]*>/)
  assert.ok(appTag, '找不到 app.js 的 script 标签')
  assert.ok(mainTag, '找不到 main.tsx 的 script 标签')
  // 这就是整个问题的前提：module 会被延后到文档解析完之后
  assert.ok(!/type=["']module["']/.test(appTag[0]), 'app.js 变成 module 了，握手假设需要重新评估')
  assert.ok(/type=["']module["']/.test(mainTag[0]), 'main.tsx 应是 module')
})

test('app.js 不再用 if (window.WyckoffReact) 来接 hooks', () => {
  const src = readFileSync(R('app.js'), 'utf8')
  // 这个写法恒为假。允许出现在别处（例如调用页面构建），但不能用来包住 setHooks。
  const badGuard = /if\s*\(\s*window\.WyckoffReact\s*\)\s*\{\s*\n\s*window\.WyckoffReact\.setHooks/
  assert.ok(!badGuard.test(src), '又回到了依赖执行顺序的写法')
  assert.ok(src.includes('WyckoffPendingHooks'), 'app.js 应把 hooks 停放到全局等 React 取')
})

test('main.tsx 会主动取走 app.js 停放的 hooks', () => {
  const src = readFileSync(R('main.tsx'), 'utf8')
  assert.ok(src.includes('WyckoffPendingHooks'), 'main.tsx 没去取 pending hooks')
  // 取的动作必须在 window.WyckoffReact 赋值之后，否则 app.js 那侧的同步分支拿不到
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
