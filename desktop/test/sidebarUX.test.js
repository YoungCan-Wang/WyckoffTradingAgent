'use strict'

const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')

const R = (...parts) => join(__dirname, '..', 'src', 'renderer', ...parts)

test('侧栏只有一个控制按钮，并在展开与收起位置之间移动', () => {
  // 外壳转 React 后按钮成了一个 JSX 片段，由 App 决定放进侧栏还是顶栏 ——
  // 仍然只有一个实例（两个会带来重复 tab 停留点和互相矛盾的状态）。
  const app = readFileSync(R('components/App.tsx'), 'utf8')
  const side = readFileSync(R('components/Sidebar.tsx'), 'utf8')
  const top = readFileSync(R('components/TopBar.tsx'), 'utf8')

  assert.equal((app.match(/className="icb side-toggle"/g) || []).length, 1, '开关按钮应只有一个实例')
  assert.match(app, /data-lucide="panel-left-open"/)
  assert.match(app, /data-lucide="panel-left-close"/)
  // 同一个片段被交给两处槽位之一
  assert.match(app, /toggleSlot=\{toggle\}/)
  assert.match(app, /toggleSlot=\{sideOpen \? null : toggle\}/)
  assert.match(side, /\{toggleSlot\}/)
  assert.match(top, /\{toggleSlot\}/)
})

test('首次打开按窗口宽度决定侧栏，之后尊重用户选择', () => {
  const app = readFileSync(R('components/App.tsx'), 'utf8')

  assert.match(app, /if \(saved !== null\) return saved === '1'/)
  assert.match(app, /return window\.innerWidth >= 1180/)
})

test('产物面板可收起但不销毁 tab，空面板不会被手动打开', () => {
  // 面板仍归命令式的 shell.js（canvas / 原生 view / sandbox iframe）。
  const shell = readFileSync(R('shell.js'), 'utf8')
  const tabs = readFileSync(R('tabs.js'), 'utf8')

  assert.match(shell, /if \(!pane\.count\(\)\) return/)
  assert.match(shell, /document\.getElementById\('btn-pane'\)\.onclick = \(\) => setPane\(false\)/)
  assert.match(shell, /setPane\(true\)\s*\n\s*pane\.showActive\(\)/)
  assert.match(tabs, /count \(\) \{\s*return this\.tabs\.length/)
  assert.match(tabs, /showActive \(\) \{/)
})
