const assert = require('node:assert/strict')
const test = require('node:test')
const { sprintf } = require('../vendor/sprintf-js')

test('sprintf keeps normal precision', () => {
  assert.equal(sprintf('%.2f', 1), '1.00')
  assert.equal(sprintf('%s', 'ok'), 'ok')
})

test('sprintf clamps hostile precision instead of throwing', () => {
  assert.doesNotThrow(() => sprintf('%.9999999f', 1.25))
  assert.doesNotThrow(() => sprintf('%.9999999e', 1.25))
  assert.doesNotThrow(() => sprintf('%.9999999g', 1.25))
  assert.equal(sprintf('%.9999999f', 1.25).split('.')[1].length, 100)
})
