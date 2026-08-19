'use strict'

const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')

const R = (...parts) => join(__dirname, '..', 'src', 'renderer', ...parts)

test('侧栏只有一个控制按钮，并在展开与收起位置之间移动', () => {
  const html = readFileSync(R('index.html'), 'utf8')
  const app = readFileSync(R('app.js'), 'utf8')

  assert.equal((html.match(/id="btn-side"/g) || []).length, 1)
  assert.match(html, /id="side-toggle-slot"/)
  assert.match(html, /id="thread-toggle-slot"/)
  assert.match(html, /data-lucide="panel-left-open"/)
  assert.match(html, /data-lucide="panel-left-close"/)
  assert.match(app, /const targetSlot = on \? sideSlot : threadSideSlot/)
  assert.match(app, /targetSlot\.appendChild\(sideButton\)/)
})

test('首次打开按窗口宽度决定侧栏，之后尊重用户选择', () => {
  const app = readFileSync(R('app.js'), 'utf8')

  assert.match(app, /if \(saved !== null\) return saved === '1'/)
  assert.match(app, /return window\.innerWidth >= 1180/)
})

test('产物面板可收起但不销毁 tab，空面板不会被手动打开', () => {
  const app = readFileSync(R('app.js'), 'utf8')
  const tabs = readFileSync(R('tabs.js'), 'utf8')

  assert.match(app, /if \(!pane\.count\(\)\) return/)
  assert.match(app, /document\.getElementById\('btn-pane'\)\.onclick = \(\) => setPane\(false\)/)
  assert.match(app, /setPane\(true\)\s*\n\s*pane\.showActive\(\)/)
  assert.match(tabs, /count \(\) \{\s*return this\.tabs\.length/)
  assert.match(tabs, /showActive \(\) \{/)
})
