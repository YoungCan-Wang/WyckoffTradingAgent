'use strict'

const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')
const pkg = require('../package.json')
const { DaemonRunner } = require('../src/daemon-runner')

test('packaging always rebuilds the renderer and bundled Python runtime', () => {
  assert.equal(pkg.scripts['build:py'], 'node ../scripts/run_build_python_ipc.cjs')
  assert.match(pkg.scripts['package:prepare'], /build:ui\s+&&\s+npm run build:py/)
  assert.match(pkg.scripts.pack, /^npm run package:prepare\s+&&/)
  assert.match(pkg.scripts.dist, /^npm run package:prepare\s+&&/)
})

test('candidate installer names identify OS and architecture', () => {
  assert.equal(pkg.build.artifactName, '${productName}-${version}-${os}-${arch}.${ext}')
  assert.equal(pkg.build.afterPack, 'build/after-pack.cjs')
})

test('Python bundle uses frozen project dependencies and pinned build tools', () => {
  const source = fs.readFileSync(
    path.join(__dirname, '..', '..', 'scripts', 'build_python_ipc.py'),
    'utf8'
  )
  assert.match(source, /UV_VERSION = "\d+\.\d+\.\d+"/)
  assert.match(source, /PYINSTALLER_VERSION = "\d+\.\d+\.\d+"/)
  assert.match(source, /"sync", "--active", "--frozen", "--no-dev"/)
  assert.doesNotMatch(source, /pip", "install", "--upgrade"/)
})

test('PyInstaller hidden imports use current Supabase package names', () => {
  const source = fs.readFileSync(
    path.join(__dirname, '..', '..', 'packaging', 'wyckoff-ipc.spec'),
    'utf8'
  )
  assert.match(source, /"supabase_auth"/)
  assert.match(source, /"supabase_functions"/)
  assert.doesNotMatch(source, /"gotrue"|"supafunc"/)
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
