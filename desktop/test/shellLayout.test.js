'use strict'

// 外壳的布局契约。
//
// 转 React 时我给 .win 里加了个 #root 挂载点，于是 .side 和 .thread 被降了
// 一层 —— .win 的 flex 只作用于直接子元素，两者不再并排，界面变成侧栏在上、
// 会话区在下。所有既有测试都是绿的：它们检查「元素存在」「文案正确」「无坏
// 文本」，没有一条检查「布局关系」。是截图才发现的。
//
// 这组测试锁住这个关系：谁再加一层包装盒子，就必须同时保证它不成为布局项。
const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')

const R = (...p) => join(__dirname, '..', 'src', 'renderer', ...p)

test('.win 是 flex 容器，侧栏与会话区必须是它的 flex 项', () => {
  const css = readFileSync(R('app.css'), 'utf8')
  assert.match(css, /\.win \{[^}]*display: flex/, '.win 应是 flex 容器')
  // 侧栏靠 flex: none + 固定宽度占左侧，会话区靠 flex: 1 吃掉剩余
  assert.match(css, /\.side \{[^}]*flex: none/s, '侧栏应是 flex: none')
  assert.match(css, /\.thread \{[^}]*flex: 1/s, '会话区应是 flex: 1')
})

test('React 挂载点不能成为布局盒子', () => {
  const html = readFileSync(R('index.html'), 'utf8')
  const css = readFileSync(R('app.css'), 'utf8')

  // 挂载点在 .win 里面 —— 这是它会干扰 flex 的前提
  assert.match(html, /<div class="win">[\s\S]{0,400}id="root"/, '#root 应在 .win 内')
  // 所以它必须 display:contents，让子元素直接参与 .win 的 flex
  assert.match(
    css,
    /\.shell-root \{[^}]*display: contents/,
    '#root 必须是 display:contents，否则侧栏和会话区会上下堆叠'
  )
  assert.match(html, /id="root" class="shell-root"/, '#root 要带上 shell-root 类')
})

test('产物面板与拖拽条仍是 .win 的直接子元素', () => {
  const html = readFileSync(R('index.html'), 'utf8')
  // 它们参与同一个 flex 行；被包进别的盒子里会让面板宽度失控
  const win = html.slice(html.indexOf('<div class="win">'), html.indexOf('</div>\n\n<script'))
  const paneAt = win.indexOf('id="pane"')
  const resizerAt = win.indexOf('id="pane-resizer"')
  assert.ok(resizerAt > 0 && paneAt > 0, '拖拽条与面板都应在 .win 内')
  assert.ok(resizerAt < paneAt, '拖拽条应在面板之前（它分隔会话区与面板）')
})

test('侧栏收起是整块卸载，而不是留一个 0 宽的空盒子', () => {
  // App 用条件渲染而非 CSS 隐藏 —— 所以 .win.side-off 那条旧规则不再是唯一
  // 依赖。这里锁住条件渲染本身。
  const app = readFileSync(R('components/App.tsx'), 'utf8')
  assert.match(app, /sideOpen \? \(\s*<Sidebar/, '侧栏应按 sideOpen 条件渲染')
})
