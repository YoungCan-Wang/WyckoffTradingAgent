'use strict'

const { spawn } = require('node:child_process')
const path = require('node:path')
const fs = require('node:fs')
const readline = require('node:readline')

const IS_WINDOWS = process.platform === 'win32'
const READY_TIMEOUT_MS = 60_000
const RESTART_DELAY_MS = 1_500
const MAX_RESTARTS = 5
// SIGTERM 之后等多久改用 SIGKILL。够 Python 跑完 atexit/落盘，又不至于让退出
// 卡住太久 —— 应用退出时用户已经点了关闭。
const SIGKILL_DELAY_MS = 3_000

/**
 * Windows 上强杀整棵进程树。
 *
 * 必须挂 error 监听：spawn 失败（taskkill 不在 PATH、被组策略拦下、进程已经
 * 没了）会**异步**发 error 事件，没人接就是未捕获异常 —— 在主进程里等于整个
 * 应用崩掉。而这条路径只在 Windows 上跑，我们平时全在 macOS 上测，
 * 崩了也看不见。
 *
 * 回收失败本身不值得打扰用户：应用正在退出，最坏结果是留一个孤儿进程。
 */
function killTreeWindows (pid, onLog) {
  try {
    const proc = spawn('taskkill', ['/pid', String(pid), '/f', '/t'], { windowsHide: true })
    proc.on('error', (err) => {
      if (onLog) onLog(`taskkill 失败（pid=${pid}）: ${err.message}`)
    })
  } catch (err) {
    if (onLog) onLog(`taskkill 无法启动（pid=${pid}）: ${err.message}`)
  }
}

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
   * 打包后的自包含 IPC 二进制，由 PyInstaller 产出、electron-builder 放进
   * resources/。存在即说明这是分发版：用户机器上没有仓库也没有 venv。
   */
  bundledBinary () {
    if (!process.resourcesPath) return ''
    const name = IS_WINDOWS ? 'wyckoff-ipc.exe' : 'wyckoff-ipc'
    const candidate = path.join(process.resourcesPath, 'wyckoff-ipc', name)
    return fs.existsSync(candidate) ? candidate : ''
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
    // 分发版优先用打包好的二进制；开发时它不存在，走仓库 venv + `-m cli ipc`。
    const bundled = this.bundledBinary()
    const python = bundled || this.pythonPath()
    this.lastPython = python
    const args = bundled ? [] : ['-m', 'cli', 'ipc']
    // 打包后没有仓库可言，用二进制所在目录当 cwd。
    const cwd = bundled ? path.dirname(bundled) : this.repoRoot

    // No shell: avoids Windows quoting/injection issues entirely.
    this.child = spawn(python, args, {
      cwd,
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
      this.status({ state: 'error', reason: 'spawn_failed', detail: `spawn failed: ${err.message}` })
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
        this.status({ state: 'error', reason: 'timeout', detail: 'python did not signal ready within 60s' })
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
      if (message.type === 'end') {
        const entry = this.pending.get(id)
        if (entry) clearTimeout(entry.timer)
        this.pending.delete(id)
      } else {
        // 流式请求会连着发很多事件；每一个都说明它还活着，重置静默计时。
        this.touchPending(id)
      }
      this.onEvent(message)
    }
  }

  handleExit (code, signal) {
    this.ready = false
    this.child = null
    clearTimeout(this.readyTimer)

    this.failPending('child_exited', 'Python 进程已退出')

    if (this.stopping) {
      this.status({ state: 'stopped' })
      return
    }
    // Status carries a machine `reason` (front-end picks the localized copy)
    // and a technical `detail` (logged, never shown). The user never sees exit
    // codes, interpreter paths, or env-var hints.
    if (!this.everReady) {
      // A child that dies before ever signalling ready is misconfigured, not
      // crashed — retrying just spins. Report it instead.
      this.status({
        state: 'error',
        reason: 'exited_early',
        detail: `python exited before ready (code=${code}, interpreter=${this.lastPython})`
      })
      return
    }
    if (this.restarts >= MAX_RESTARTS) {
      this.status({
        state: 'error',
        reason: 'gave_up',
        detail: `python exited ${MAX_RESTARTS}× consecutively, auto-restart abandoned`
      })
      return
    }
    this.restarts += 1
    this.status({
      state: 'restarting',
      reason: 'crashed',
      detail: `python exited (code=${code}, signal=${signal}), restart #${this.restarts}`
    })
    setTimeout(() => this.start(), RESTART_DELAY_MS)
  }

  failPending (code, message) {
    // Every request must terminate exactly once; otherwise collect() and chat
    // streams in the renderer retain callbacks forever after a process change.
    for (const [id, entry] of this.pending) {
      // 先摘计时器：否则进程重启之后它还会给同一个 id 再补一次 error/end
      if (entry && entry.timer) clearTimeout(entry.timer)
      this.onEvent({ id, type: 'error', code, message })
      this.onEvent({ id, type: 'end' })
    }
    this.pending.clear()
  }

  /**
   * 一轮请求的**静默**超时：多久没有任何事件才算它挂了。
   *
   * 刻意不是「总时长」上限：对话和漏斗本来就可能跑十几分钟，按总时长砍会把
   * 正常的长任务杀掉。这里要抓的是另一种情况 —— 请求发出去之后再也没有任何
   * 动静（Python 侧卡在某个无限等待的网络调用上，进程还活着）。
   *
   * 那种情况下 pending 里的 id 永远不删、事件流永不发 end，前端永久转圈；
   * 而 Python 侧是 ThreadPoolExecutor(max_workers=4)，几个挂起的请求就占满
   * 线程池，之后所有请求静默排队。原来只有进程退出/重启才会清理。
   */
  static IDLE_TIMEOUT_MS = 180_000

  send (method, params) {
    if (!this.child || !this.ready || !this.child.stdin.writable) {
      return { ok: false, error: 'Python 尚未就绪' }
    }
    const id = this.nextId++
    this.pending.set(id, { method, timer: this.armIdleTimer(id, method) })
    this.child.stdin.write(`${JSON.stringify({ id, method, params: params || {} })}\n`)
    return { ok: true, id }
  }

  /** 起一个静默计时器；每来一个事件就由 touchPending 重置。 */
  armIdleTimer (id, method) {
    return setTimeout(() => {
      const entry = this.pending.get(id)
      if (!entry) return
      this.pending.delete(id)
      const seconds = Math.round(PythonBridge.IDLE_TIMEOUT_MS / 1000)
      this.onStatus({ state: 'log', message: `[bridge] ${method} (#${id}) ${seconds}s 无响应，判定为挂起` })
      // 必须补上 end：前端的那一轮靠它收尾，只发 error 会留下永久转圈的 UI。
      this.onEvent({
        id,
        type: 'error',
        code: 'request_timeout',
        message: `${method} 超过 ${seconds} 秒没有任何响应，已放弃这一轮`
      })
      this.onEvent({ id, type: 'end' })
    }, PythonBridge.IDLE_TIMEOUT_MS)
  }

  /** 收到该 id 的任何事件都重置它的静默计时器。 */
  touchPending (id) {
    const entry = this.pending.get(id)
    if (!entry) return
    clearTimeout(entry.timer)
    entry.timer = this.armIdleTimer(id, entry.method)
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
        // /f 本身就是强杀，没有再升级的余地。
        killTreeWindows(child.pid, (m) => this.onStatus({ state: 'log', message: `[bridge] ${m}` }))
        return
      }
      child.kill('SIGTERM')
      // SIGTERM 是「请你退」，Python 卡在原生代码里（C 扩展、阻塞的 socket
      // 读）时收不到、也就不会退 —— 只发一次等于留下孤儿进程。给它一段时间
      // 自己走，然后强杀。
      setTimeout(() => {
        // exit 事件会把 this.child 置空；还在就说明 SIGTERM 没起作用。
        if (this.child !== child && this.child !== null) return
        if (child.exitCode !== null || child.signalCode !== null) return
        this.onStatus({ state: 'log', message: `[bridge] SIGTERM 未生效，改用 SIGKILL 回收 pid=${child.pid}` })
        try {
          child.kill('SIGKILL')
        } catch {
          /* 已经没了 */
        }
      }, SIGKILL_DELAY_MS)
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
    this.failPending('backend_restarted', '后端已重启，请重新发起操作')
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
        killTreeWindows(old.pid, (m) => this.onStatus({ state: 'log', message: `[bridge] ${m}` }))
      } else {
        try { old.kill('SIGTERM') } catch { /* already dead */ }
        // 和 stop() 一样要升级到 SIGKILL：卡在原生调用里的 Python 收不到
        // SIGTERM，只发一次就会留下孤儿 —— 而它还攥着旧的登录身份和文件锁，
        // 下一个进程起来后两者并存。restart() 是用户点「重启」时走的路径,
        // 恰恰常用在「桥卡住了」的时候，也就是最可能收不到信号的时候。
        setTimeout(() => {
          if (old.exitCode !== null || old.signalCode !== null) return
          this.onStatus({ state: 'log', message: `[bridge] 重启：SIGTERM 未生效，改用 SIGKILL 回收 pid=${old.pid}` })
          try { old.kill('SIGKILL') } catch { /* 已经没了 */ }
        }, SIGKILL_DELAY_MS)
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
