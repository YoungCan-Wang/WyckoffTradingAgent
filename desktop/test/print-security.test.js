'use strict'

const assert = require('node:assert/strict')
const test = require('node:test')
const { PRINT_WEB_PREFERENCES, blockPrintNetwork } = require('../src/print-security')

test('print window disables scripts and Node access', () => {
  assert.equal(PRINT_WEB_PREFERENCES.javascript, false)
  assert.equal(PRINT_WEB_PREFERENCES.nodeIntegration, false)
  assert.equal(PRINT_WEB_PREFERENCES.contextIsolation, true)
  assert.equal(PRINT_WEB_PREFERENCES.sandbox, true)
})

test('print window blocks external network resources', () => {
  let handler
  blockPrintNetwork({ webRequest: { onBeforeRequest: (callback) => { handler = callback } } })
  let result
  handler({ url: 'https://attacker.example/beacon' }, (value) => { result = value })
  assert.deepEqual(result, { cancel: true })
  handler({ url: 'data:text/html,report' }, (value) => { result = value })
  assert.deepEqual(result, { cancel: false })
})
