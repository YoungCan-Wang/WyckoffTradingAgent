'use strict'

const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')
const pkg = require('../package.json')
const { DaemonRunner } = require('../src/daemon-runner')

test('packaging always rebuilds the renderer and bundled Python runtime', () => {
  assert.match(pkg.scripts['package:prepare'], /build:ui\s+&&\s+npm run build:py/)
  assert.match(pkg.scripts.pack, /^npm run package:prepare\s+&&/)
  assert.match(pkg.scripts.dist, /^npm run package:prepare\s+&&/)
})

test('packaged scheduler uses the bundled executable daemon entrypoint', () => {
  const runner = new DaemonRunner({
    repoRoot: '/repo',
    python: '/repo/.venv/bin/python',
    bundledBinary: '/app/resources/wyckoff-ipc/wyckoff-ipc'
  })
  assert.equal(runner.command, '/app/resources/wyckoff-ipc/wyckoff-ipc')
  assert.deepEqual(runner.args, ['--daemon'])
  assert.equal(runner.cwd, '/app/resources/wyckoff-ipc')
})

test('macOS activate path guards a missing scheduler during recovery', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'src', 'main.js'), 'utf8')
  assert.match(source, /if \(daemon && !daemon\.child\) daemon\.start\(\)/)
})
