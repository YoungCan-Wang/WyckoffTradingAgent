'use strict'

// shell.js 先注册命令式能力，main.tsx 再挂 React 外壳。两边只通过显式的
// window.WyckoffShell 接口交互，不保留旧 app.js 的双向挂载协议。
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

test('React 入口不保留已删除 app.js 的挂载协议', () => {
  const src = readFileSync(R('main.tsx'), 'utf8')
  for (const legacy of ['WyckoffPendingHooks', 'WyckoffPendingChatHost', 'mountSettings', 'mountChat']) {
    assert.ok(!src.includes(legacy), `仍残留旧外壳协议 ${legacy}`)
  }
})

test('换账号时对话状态必须清空', () => {
  // 复审发现的 P1：useChat 原来**完全没有**监听 account-changed（grep 计数 0），
  // 而 App.tsx 派发了它、持仓和归因都接了，只有对话没接。
  const c = readFileSync(R('lib', 'useChat.ts'), 'utf8')
  assert.match(c, /wyckoff:account-changed/,
    'useChat 必须监听换账号事件 —— 否则上一个账号的对话留在界面上')
  // 最关键的是 sessionId：留着它的话新账号发消息会带上旧 id，
  // 后端据此恢复历史 —— 那是跨账号泄漏的触发路径。
  const handler = c.slice(c.indexOf('clearForAccountChange'), c.indexOf('const reset'))
  assert.match(handler, /setSessionId\(''\)/, '必须清 sessionId —— 它是泄漏的触发点')
  assert.match(handler, /setTurns\(\[\]\)/, '必须清 turns')
  assert.match(handler, /artifactsApi\.reset\(\)/, '必须清产物 —— 上一个人的报告不能留着')
})

test('产物有独立的 reset，且与 beginTurn 区分', () => {
  // beginTurn 只重置「本轮」标记，产物列表照留（同一个人的上一轮该看得到）。
  // 换账号要连列表一起清，所以需要单独的 reset。
  const a = readFileSync(R('lib', 'useArtifacts.ts'), 'utf8')
  assert.match(a, /reset: \(\) => void/, 'ArtifactsApi 要暴露 reset')
  assert.match(a, /setArtifacts\(\[\]\)/, 'reset 要清空产物列表')
  // 面板是独立组件，不发事件的话上一个账号的页签会继续挂着
  const resetBody = a.slice(a.indexOf('const reset = useCallback'))
  assert.match(resetBody, /wyckoff:artifacts/, 'reset 要让面板收起')
})
