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
  assert.match(dlg, /shell\.inert = true/)
  assert.match(dlg, /shell\.inert = false/)
  assert.match(dlg, /e\.key !== 'Tab'/)
  assert.match(app, /opener/)
  assert.match(app, /target\?\.isConnected\) target\.focus\(\)/)
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
