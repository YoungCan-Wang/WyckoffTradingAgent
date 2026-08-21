'use strict'

const { existsSync } = require('node:fs')
const { join } = require('node:path')
const { spawnSync } = require('node:child_process')

const root = join(__dirname, '..')
const script = join(__dirname, 'build_python_ipc.py')
const venvPython = process.platform === 'win32'
  ? join(root, '.venv', 'Scripts', 'python.exe')
  : join(root, '.venv', 'bin', 'python')
const candidates = [
  process.env.PYTHON,
  existsSync(venvPython) ? venvPython : null,
  process.platform === 'win32' ? 'python' : 'python3.11',
  process.platform === 'win32' ? null : 'python3'
].filter(Boolean)

for (const command of candidates) {
  const result = spawnSync(command, [script], { cwd: root, stdio: 'inherit' })
  if (result.error?.code === 'ENOENT') continue
  if (result.error) throw result.error
  process.exit(result.status ?? 1)
}

throw new Error('Python 3.11+ was not found; set PYTHON to an interpreter path')
