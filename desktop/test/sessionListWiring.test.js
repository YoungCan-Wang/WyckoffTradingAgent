'use strict'

// 会话列表的接线。按项目惯例做源码结构断言（没有 jsdom，组件不真渲染）——
// 纯逻辑的行为覆盖在 sessions.test.js。
const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')

const src = (...p) => readFileSync(join(__dirname, '..', 'src', ...p), 'utf8')
const LIST = src('renderer', 'components', 'SessionList.tsx')
const ROW = src('renderer', 'components', 'SessionRow.tsx')
const SIDE = src('renderer', 'components', 'Sidebar.tsx')
const APP = src('renderer', 'components', 'App.tsx')
const CHAT = src('renderer', 'components', 'ChatView.tsx')
const HOOK = src('renderer', 'lib', 'useChat.ts')
const CSS = src('renderer', 'app.css')
const LOCALES = src('renderer', 'locales.js')

test('列表用语言无关的选择器，测试和 e2e 不依赖译文', () => {
  assert.match(LIST, /data-testid="session-list"/)
  assert.match(ROW, /data-session=\{session\.session_id\}/)
  assert.match(ROW, /data-active=/)
})

test('当前会话标了 aria-current', () => {
  assert.match(ROW, /aria-current=\{active \? 'true' : undefined\}/)
})

test('行内操作按钮阻止冒泡', () => {
  // 行本身可点（切换会话）。不 stopPropagation 的话点「删除」会顺带切过去。
  const handlers = ROW.match(/onClick=\{\(e\) => \{ e\.stopPropagation\(\)/g) || []
  assert.ok(handlers.length >= 2, `期望多处 stopPropagation，实际 ${handlers.length}`)
})

test('删除前有确认', () => {
  // 不可撤销。项目里四处删除都用原生 confirm。
  assert.match(ROW, /window\.confirm\(t\('session\.deleteConfirm'\)\)/)
})

test('重命名支持 Esc 放弃', () => {
  // 没有它，用户一旦开始编辑就只能提交或点走。
  assert.match(ROW, /e\.key === 'Escape'/)
})

test('空标题不写回', () => {
  assert.match(ROW, /if \(next && next !== displayTitle/)
})

test('侧边栏保留固定入口，会话作为插槽插在下面', () => {
  assert.match(SIDE, /sessionSlot\?: React\.ReactNode/)
  // nav 仍在，且插槽在它之后
  const navAt = SIDE.indexOf('</nav>')
  const slotAt = SIDE.indexOf('{sessionSlot}')
  assert.ok(navAt > 0 && slotAt > navAt, '会话区必须在固定导航之后')
})

test('固定导航不参与滚动', () => {
  // 常驻导航被会话列表挤出视野就等于没有。
  assert.match(CSS, /\.nav \{ padding: 0 7px; flex: none; \}/)
  assert.match(CSS, /\.sess-scroll \{[^}]*overflow-y: auto/)
})

test('滚动容器设了 min-height:0', () => {
  // flex 子项默认 min-height:auto，不设它内容会撑破容器而不是出现滚动条。
  assert.match(CSS, /\.sess-wrap \{[^}]*min-height: 0/s)
})

test('App 持有会话状态，不是列表自己管', () => {
  // 切换由侧边栏发起、由 ChatView 执行，两边都不拥有这个状态。
  assert.match(APP, /const \[activeSession, setActiveSession\]/)
  assert.match(APP, /<SessionList/)
  assert.match(APP, /onSwitch=\{switchSession\}/)
})

test('新建分析后刷新列表', () => {
  // 新会话要立刻出现，否则用户以为按钮没生效。
  assert.match(APP, /setSessionNonce\(\(n\) => n \+ 1\)/)
})

test('ChatView 通过 window 桥暴露 loadSession', () => {
  // 会话列表在侧边栏、对话状态在 ChatView，沿用已有桥而不引全局 store。
  assert.match(CHAT, /loadSession: async \(id: string\)/)
  assert.match(CHAT, /sessionId: \(\) => chat\.sessionId/)
})

test('send 带上 session_id', () => {
  // 不带的话后端用它自己的「活跃会话」，可能已经和前端不一致。
  assert.match(HOOK, /if \(sessionId\) params\.session_id = sessionId/)
})

test('一轮在跑时不切换会话', () => {
  // 那一轮的事件仍按 id 分发到旧 turn，而 turns 已被替换 —— 回复会凭空消失。
  assert.match(HOOK, /if \(busy \|\| !ready \|\| !id\) return false/)
})

test('reset 记下新会话 id', () => {
  assert.match(HOOK, /setSessionId\(String\(result\.session_id \|\| ''\)\)/)
})

test('删除当前会话时有落脚处', () => {
  // 不给的话下一轮对话会写进一个刚被删掉的 id。
  assert.match(LIST, /nextAfterDelete\(rows, id, activeId\)/)
  assert.match(LIST, /else onNeedNew\(\)/)
})

test('重命名和置顶做乐观更新', () => {
  // 等一次往返再改会让输入框刚提交完还显示旧值。
  assert.match(LIST, /setRows\(\(prev\) => prev\.map\(\(s\) => \(s\.session_id === id \? \{ \.\.\.s, title \}/)
})

test('文案中英齐全', () => {
  global.window = {}
  delete require.cache[require.resolve('../src/renderer/locales.js')]
  require('../src/renderer/locales.js')
  const { zh, en } = global.window.WyckoffLocales
  const keys = Object.keys(zh).filter((k) => k.startsWith('session.'))
  assert.ok(keys.length >= 12, `session 文案太少：${keys.length}`)
  const missing = keys.filter((k) => !(k in en))
  assert.deepEqual(missing, [], `英文缺少：${missing.join(', ')}`)
  assert.ok(zh['chat.loadFailed'] && en['chat.loadFailed'], '读取失败提示要双语')
})

test('搜索框只在会话多时出现', () => {
  // 三五个会话时一个搜索框是纯噪音。
  assert.match(LIST, /rows\.length > 6 \? \(/)
})
