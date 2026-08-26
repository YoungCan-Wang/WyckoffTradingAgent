/**
 * 对话界面的基本交互:右键复制、回复操作栏、消息对齐。
 */
'use strict'
const test = require('node:test')
const assert = require('node:assert')
const fs = require('node:fs')
const path = require('node:path')

const SRC = (rel) => fs.readFileSync(path.join(__dirname, '..', 'src', rel), 'utf8')
const R = (rel) => SRC(path.join('renderer', rel))
const CODE = (rel) => R(rel).replace(/\/\*[\s\S]*?\*\//g, '')
  .split('\n').filter((l) => !l.trim().startsWith('//')).join('\n')

test('有右键菜单 —— Electron 默认不给', () => {
  // 打包成应用之后就没有 Chrome 自带的右键菜单了，选中文字右键不出「复制」。
  const main = SRC('main.js')
  assert.match(main, /function attachContextMenu/, '缺少右键菜单')
  assert.match(main, /contents\.on\('context-menu'/)
  assert.match(main, /attachContextMenu\(mainWindow\.webContents\)/, '没挂到主窗口上')
})

test('右键菜单用 role 而不是自己调剪贴板', () => {
  // role 自带快捷键显示、禁用态和本地化，走的是 Chromium 编辑命令。
  const main = SRC('main.js')
  const fn = main.slice(main.indexOf('function attachContextMenu'))
  assert.match(fn, /role: 'copy'/)
  assert.match(fn, /role: 'paste'/, '输入框要能粘贴')
  assert.match(fn, /props\.isEditable/, '输入框和只读文本的菜单不该一样')
})

test('没有可做的事就不弹菜单', () => {
  // 一个只有灰项的菜单比没有菜单更让人困惑。
  const main = SRC('main.js')
  const fn = main.slice(main.indexOf('function attachContextMenu'))
  assert.match(fn, /if \(!items\.length\) return/)
})

test('回复下方有复制按钮和时间', () => {
  const v = CODE('components/ChatStream.tsx')
  assert.match(v, /function ReplyFoot/)
  assert.match(v, /reply-copy/)
  assert.match(v, /reply-time/)
  assert.match(v, /navigator\.clipboard\.writeText/)
})

test('复制只取正文，不带工具行', () => {
  // 粘出去带一串「✓ 持仓」是噪音，用户要的是回答本身。
  const v = R('components/ChatStream.tsx')
  const fn = v.slice(v.indexOf('function ReplyFoot'), v.indexOf('function formatTime'))
  assert.match(fn, /filter\(\(b\) => b\.kind === 'text'\)/, '只复制 text 块')
})

test('还在跑时不显示操作栏', () => {
  // 复制一段还在变的文本没有意义，时间也还没定。
  const v = CODE('components/ChatStream.tsx')
  assert.match(v, /!turn\.live && turn\.blocks\.some\(\(b\) => b\.kind === 'text'\)/)
})

test('复制有成功反馈', () => {
  // 没有反馈的话用户不知道成没成，会再点一次。
  const v = R('components/ChatStream.tsx')
  const fn = v.slice(v.indexOf('function ReplyFoot'), v.indexOf('function formatTime'))
  assert.match(fn, /setCopied\(true\)/)
  assert.match(fn, /t\('chat\.copied'\)/)
})

test('复制按钮键盘可达', () => {
  // 只在 hover 时出现的功能，对键盘用户等于不存在。
  const css = R('app.css')
  assert.match(css, /\.reply-copy:focus-visible \{[^}]*opacity: 1/)
})

test('跨天的时间带日期', () => {
  // 昨天的对话显示「14:30」会让人误以为是刚才。
  const v = R('components/ChatStream.tsx')
  const fn = v.slice(v.indexOf('function formatTime'))
  assert.match(fn, /sameDay/)
  assert.match(fn, /getMonth\(\) \+ 1/, '跨天要补月/日')
})

test('用户提问靠右，AI 回复全宽', () => {
  // 不是 IM 那种左右分栏：AI 回复里的 markdown 表格、代码块、工具行和审批卡片
  // 是固有宽度内容，压到半宽会横向滚动。用户提问是短文本，靠右收窄更好扫。
  const css = R('app.css')
  assert.match(css, /\.msg\.u \{[^}]*justify-content: flex-end/)
  assert.match(css, /\.msg\.u \.bd \{[^}]*max-width/, '用户气泡要有宽度上限')
  // AI 侧不该被加上宽度限制
  assert.ok(!/\.msg\.a \.bd \{[^}]*max-width/.test(css), 'AI 回复不该收窄')
  const v = CODE('components/ChatStream.tsx')
  assert.match(v, /className="msg u"/, '用户消息要带 .u')
})
