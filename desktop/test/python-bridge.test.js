'use strict'

const assert = require('node:assert/strict')
const test = require('node:test')
const { PythonBridge } = require('../src/python-bridge')

test('failing pending requests emits an error and terminal event', () => {
  const events = []
  const bridge = new PythonBridge({
    repoRoot: '/tmp',
    onEvent: (event) => events.push(event),
    onStatus: () => {}
  })
  bridge.pending.set(11, 'chat')
  bridge.pending.set(12, 'portfolio')

  bridge.failPending('backend_restarted', 'restart')

  assert.deepEqual(events, [
    { id: 11, type: 'error', code: 'backend_restarted', message: 'restart' },
    { id: 11, type: 'end' },
    { id: 12, type: 'error', code: 'backend_restarted', message: 'restart' },
    { id: 12, type: 'end' }
  ])
  assert.equal(bridge.pending.size, 0)
})
