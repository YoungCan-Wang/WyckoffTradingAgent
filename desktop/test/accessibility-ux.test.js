'use strict'

const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const test = require('node:test')

const renderer = (...parts) => join(__dirname, '..', 'src', 'renderer', ...parts)
const source = (name) => readFileSync(renderer(name), 'utf8')

test('settings dialog traps focus, makes the app inert, and restores its opener', () => {
  // 外壳转 React 后这些契约搬到了 SettingsModal / App，但要求不变：
  // 背景 inert、Tab 在弹窗内环形、关闭后焦点还给打开它的控件。
  const dlg = source('components/SettingsModal.tsx')
  const app = source('components/App.tsx')
  assert.match(dlg, /\.side, \.thread, \.pane-resizer, \.pane/)
  assert.match(dlg, /node\.inert = open/)
  assert.doesNotMatch(dlg, /querySelector<HTMLElement>\('\.win'\)/)
  assert.match(dlg, /e\.key !== 'Tab'/)
  assert.match(app, /opener/)
  assert.match(app, /requestAnimationFrame\(\(\) => \{ if \(target\?\.isConnected\) target\.focus\(\) \}\)/)

  // 设置入口如果来自会卸载的菜单项，必须先把焦点放回稳定触发按钮。
  assert.match(source('components/AccountMenu.tsx'), /anchor\.focus\(\)[\s\S]*onSettings\(\)/)
  assert.match(source('components/ModelPicker.tsx'), /btn\.current\?\.focus\(\)[\s\S]*openSettings/)
})

test('open and account menus implement desktop keyboard navigation', () => {
  // 两个菜单各自实现同一套键盘行为（上下移动、Home/End 跳首尾、Esc 关闭并
  // 把焦点还给触发按钮）。
  for (const name of ['components/OpenMenu.tsx', 'components/AccountMenu.tsx']) {
    const src = source(name)
    assert.match(src, /'ArrowDown'/, name)
    assert.match(src, /'ArrowUp'/, name)
    assert.match(src, /'Home'/, name)
    assert.match(src, /'End'/, name)
    assert.match(src, /anchor\.focus\(\)/, name)
  }
})

test('artifact panel exposes a persisted pointer and keyboard separator', () => {
  // 面板拖拽刻意留在命令式的 shell.js：它写的是 CSS 变量 + pointer capture，
  // 用 React state 表达会引入一帧延迟。
  const html = source('index.html')
  const shell = source('shell.js')
  assert.match(html, /id="pane-resizer" role="separator"/)
  assert.match(shell, /setPointerCapture/)
  assert.match(shell, /PANE_WIDTH_KEY/)
  assert.match(shell, /event\.key === 'ArrowLeft'/)
})

test('report empty state has an explicit file import action', () => {
  const viewer = source('viewer.js')
  assert.match(viewer, /fileInput\.accept/)
  assert.match(viewer, /t\('viewer\.import'\)/)
  assert.match(viewer, /fileInput\.click\(\)/)
})

test('new analysis resets the backend session instead of only navigating', () => {
  const app = source('components/App.tsx')
  const sidebar = source('components/Sidebar.tsx')
  const chat = source('lib/useChat.ts')
  assert.match(sidebar, /onClick=\{onNewAnalysis\}/)
  assert.match(app, /navigate\('chat'\)[\s\S]*WyckoffChat\?\.newAnalysis\(\)/)
  assert.match(chat, /collect\('chat_reset'\)/)
  assert.match(chat, /setTurns\(\[\]\)/)
})

test('model picker keeps the last successful settings while remounting', () => {
  const picker = source('components/ModelPicker.tsx')
  assert.match(picker, /let cachedSettings: Settings \| null = null/)
  assert.match(picker, /useState<Settings \| null>\(cachedSettings\)/)
  assert.match(picker, /if \(res\) \{[\s\S]*cachedSettings =/)
})
