'use strict'

const { spawn } = require('node:child_process')
const path = require('node:path')
const fs = require('node:fs')
const readline = require('node:readline')

const IS_WINDOWS = process.platform === 'win32'
const READY_TIMEOUT_MS = 60_000
const RESTART_DELAY_MS = 1_500
const MAX_RESTARTS = 5

/**
 * Owns the long-lived Python child. The 6s agent-stack import is paid once here,
 * not per request — that is the entire reason for a resident process.
 */
class PythonBridge {
  constructor ({ repoRoot, onEvent, onStatus, browserEndpoint }) {
    this.repoRoot = repoRoot
    this.onEvent = onEvent
    this.onStatus = onStatus
    this.browserEndpoint = browserEndpoint || null
    this.child = null
    this.ready = false
    this.everReady = false
    this.restarts = 0
    this.nextId = 1
    this.pending = new Map()
    this.stopping = false
    this.lastPython = ''
  }

  venvPython (root) {
    return IS_WINDOWS
      ? path.join(root, '.venv', 'Scripts', 'python.exe')
      : path.join(root, '.venv', 'bin', 'python')
  }

  /**
   * Prefer the repo venv. WYCKOFF_PYTHON overrides it, which is what makes a
   * git worktree usable — a worktree has no .venv of its own, and falling back
   * to a bare system python means the child dies instantly on missing deps.
   */
  pythonPath () {
    const override = process.env.WYCKOFF_PYTHON
    if (override && fs.existsSync(override)) return override

    const local = this.venvPython(this.repoRoot)
    if (fs.existsSync(local)) return local

    // .claude/worktrees/<name> -> walk up to the main checkout's venv.
    const marker = `${path.sep}.claude${path.sep}worktrees${path.sep}`
    const at = this.repoRoot.indexOf(marker)
    if (at !== -1) {
      const mainRoot = this.repoRoot.slice(0, at)
      const shared = this.venvPython(mainRoot)
      if (fs.existsSync(shared)) return shared
    }
    return IS_WINDOWS ? 'python.exe' : 'python3'
  }

  start () {
    if (this.child) return
    this.stopping = false
    const python = this.pythonPath()
    this.lastPython = python

    // No shell: avoids Windows quoting/injection issues entirely.
    this.child = spawn(python, ['-m', 'cli', 'ipc'], {
      cwd: this.repoRoot,
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
      env: {
        ...process.env,
        PYTHONUNBUFFERED: '1',
        PYTHONIOENCODING: 'utf-8',
        // In-app browser control endpoint. Passed via env rather than a file so
        // the token never lands on disk; only this child inherits it.
        ...(this.browserEndpoint
          ? {
              WYCKOFF_APP_BROWSER_URL: `http://127.0.0.1:${this.browserEndpoint.port}`,
              WYCKOFF_APP_BROWSER_TOKEN: this.browserEndpoint.token
            }
          : {})
      }
    })

    // Captured up front so every handler can distinguish THIS process from a
    // later one after a restart.
    const child = this.child

    // A spawn failure (bad interpreter, ENOENT) fires 'error' but NEVER 'exit',
    // so handleExit never runs and this.child stays set. Clear it here, or the
    // next start() bails on its `if (this.child) return` guard and the restart
    // button silently does nothing.
    this.child.on('error', (err) => {
      if (this.child === child) {
        this.child = null
        this.ready = false
        clearTimeout(this.readyTimer)
      }
      this.status({ state: 'error', message: `无法启动 Python: ${err.message}` })
    })

    // An unhandled 'error' on stdin takes down the whole main process. EPIPE
    // just means Python exited first, which is normal during shutdown/restart.
    this.child.stdin.on('error', (err) => {
      if (err && err.code === 'EPIPE') return
      console.error(`[bridge] stdin error: ${err.message}`)
    })

    readline
      .createInterface({ input: this.child.stdout, crlfDelay: Infinity })
      .on('line', (line) => this.handleLine(line))

    // Python logs and any stray print() land here — never on the protocol channel.
    readline
      .createInterface({ input: this.child.stderr, crlfDelay: Infinity })
      .on('line', (line) => this.onStatus({ state: 'log', message: line }))

    // Bind to THIS child, not this.child: after a restart the previous process
    // may exit late, and an unqualified handler would then null the new child's
    // handle (handleExit clears it unconditionally), stalling the UI.
    this.child.on('exit', (code, signal) => {
      if (this.child !== child) return
      this.handleExit(code, signal)
    })

    this.readyTimer = setTimeout(() => {
      if (!this.ready) {
        this.status({ state: 'error', message: 'Python 启动超时（60 秒）' })
      }
    }, READY_TIMEOUT_MS)

    this.status({ state: 'starting' })
  }

  handleLine (line) {
    const trimmed = line.trim()
    if (!trimmed) return
    let message
    try {
      message = JSON.parse(trimmed)
    } catch {
      // A non-JSON line means something wrote to the protocol channel.
      this.onStatus({ state: 'log', message: `[非协议输出] ${trimmed}` })
      return
    }

    if (message.type === 'ready') {
      this.ready = true
      this.everReady = true
      this.restarts = 0
      clearTimeout(this.readyTimer)
      this.status({ state: 'ready', protocol: message.protocol })
      return
    }

    const { id } = message
    if (id !== undefined && id !== null) {
      this.onEvent(message)
      if (message.type === 'end') this.pending.delete(id)
    }
  }

  handleExit (code, signal) {
    this.ready = false
    this.child = null
    clearTimeout(this.readyTimer)

    // Fail every in-flight request; the renderer must not wait forever.
    for (const id of this.pending.keys()) {
      this.onEvent({ id, type: 'error', code: 'child_exited', message: 'Python 进程已退出' })
      this.onEvent({ id, type: 'end' })
    }
    this.pending.clear()

    if (this.stopping) {
      this.status({ state: 'stopped' })
      return
    }
    // A child that dies before ever signalling ready is misconfigured, not
    // crashed — retrying just spins. Report it instead.
    if (!this.everReady) {
      this.status({
        state: 'error',
        message:
          `Python 启动即退出（code=${code}）。多半是解释器缺依赖：` +
          `当前使用 ${this.lastPython}。可设 WYCKOFF_PYTHON 指向正确的 venv。`
      })
      return
    }
    if (this.restarts >= MAX_RESTARTS) {
      this.status({ state: 'error', message: `Python 反复退出（${MAX_RESTARTS} 次），已放弃重启` })
      return
    }
    this.restarts += 1
    this.status({
      state: 'restarting',
      message: `Python 退出（code=${code} signal=${signal}），第 ${this.restarts} 次重启`
    })
    setTimeout(() => this.start(), RESTART_DELAY_MS)
  }

  send (method, params) {
    if (!this.child || !this.ready || !this.child.stdin.writable) {
      return { ok: false, error: 'Python 尚未就绪' }
    }
    const id = this.nextId++
    this.pending.set(id, method)
    this.child.stdin.write(`${JSON.stringify({ id, method, params: params || {} })}\n`)
    return { ok: true, id }
  }

  stop () {
    this.stopping = true
    const child = this.child
    // Nothing to reap. Guarding on `stopping` instead would skip the kill on a
    // restart (stop→start), leaving the old process alive.
    if (!child) return
    try {
      // Stream writes report EPIPE ASYNCHRONOUSLY via the 'error' event, so a
      // bare try/catch cannot contain it — an unhandled EPIPE here crashed the
      // main process on quit. Swallow it per-write; the child is going away.
      if (child.stdin.writable) {
        child.stdin.write('__shutdown__\n', (err) => {
          if (err && err.code !== 'EPIPE') {
            console.error(`[bridge] shutdown write failed: ${err.message}`)
          }
        })
      }
    } catch {
      /* pipe already closed */
    }
    // Windows has no real SIGTERM; taskkill is the only reliable way to reap it.
    setTimeout(() => {
      if (!this.child) return
      if (IS_WINDOWS) {
        spawn('taskkill', ['/pid', String(child.pid), '/f', '/t'], { windowsHide: true })
      } else {
        child.kill('SIGTERM')
      }
    }, 1_000)
  }

  /**
   * User-initiated restart. Must work from EVERY failure state, including the
   * ones auto-restart cannot recover from:
   *  - spawn error / hung startup: this.child may be a dead-but-set handle
   *  - "gave up after MAX_RESTARTS": the counter is exhausted
   * So we detach the current handle, reset the counter, and start fresh. The
   * detach matters — start() bails if this.child is truthy.
   */
  restart () {
    const old = this.child
    // Drop our reference first so start() is never blocked by a stale handle,
    // and a late exit from the old process cannot null the new one.
    this.child = null
    this.ready = false
    this.restarts = 0
    clearTimeout(this.readyTimer)
    if (old) {
      try {
        if (old.stdin.writable) {
          old.stdin.write('__shutdown__\n', () => {})
        }
      } catch {
        /* pipe already gone */
      }
      // Reap the old process regardless of its state.
      if (IS_WINDOWS) {
        spawn('taskkill', ['/pid', String(old.pid), '/f', '/t'], { windowsHide: true })
      } else {
        try { old.kill('SIGTERM') } catch { /* already dead */ }
      }
    }
    // Give a live child a moment to exit before respawning; a dead/never-started
    // one loses nothing by the same short wait.
    setTimeout(() => this.start(), RESTART_DELAY_MS)
  }

  status (payload) {
    this.onStatus({ ...payload, state: payload.state })
  }
}

module.exports = { PythonBridge }
