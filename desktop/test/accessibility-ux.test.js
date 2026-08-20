'use strict'

const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const test = require('node:test')

const renderer = (...parts) => join(__dirname, '..', 'src', 'renderer', ...parts)
const source = (name) => readFileSync(renderer(name), 'utf8')

test('settings dialog traps focus, makes the app inert, and restores its opener', () => {
  const app = source('app.js')
  assert.match(app, /win\.inert = true/)
  assert.match(app, /win\.inert = false/)
  assert.match(app, /event\.key !== 'Tab'/)
  assert.match(app, /settingsOpener/)
  assert.match(app, /target\.focus\(\)/)
})

test('open and account menus implement desktop keyboard navigation', () => {
  const app = source('app.js')
  assert.match(app, /function handleMenuKeys/)
  assert.match(app, /'ArrowDown'/)
  assert.match(app, /'ArrowUp'/)
  assert.match(app, /'Home'/)
  assert.match(app, /'End'/)
  assert.match(app, /trigger\.focus\(\)/)
})

test('artifact panel exposes a persisted pointer and keyboard separator', () => {
  const html = source('index.html')
  const app = source('app.js')
  assert.match(html, /id="pane-resizer" role="separator"/)
  assert.match(app, /setPointerCapture/)
  assert.match(app, /PANE_WIDTH_KEY/)
  assert.match(app, /event\.key === 'ArrowLeft'/)
})

test('report empty state has an explicit file import action', () => {
  const viewer = source('viewer.js')
  assert.match(viewer, /fileInput\.accept/)
  assert.match(viewer, /t\('viewer\.import'\)/)
  assert.match(viewer, /fileInput\.click\(\)/)
})
