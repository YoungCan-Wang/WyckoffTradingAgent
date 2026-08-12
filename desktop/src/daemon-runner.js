'use strict'

// 定时调度进程的生命周期跟随本应用：应用开着才跑，应用退出就收掉。
// 刻意不装 launchd —— 用户要的是「不开应用就不跑定时任务」。
//
// 和 PythonBridge 分开写：那个是请求/响应通道，需要 ready 握商、pending 表、
// 重启计数；这个是纯后台单向进程，混在一起只会让两种失败模式互相干扰。

const { spawn } = require('node:child_process')

const IS_WINDOWS = process.platform === 'win32'
// 退出即失败说明环境不对（缺依赖、路径错），重试只是空转。
const MAX_RESTARTS = 3
const RESTART_DELAY_MS = 2_000

class DaemonRunner {
  constructor ({ repoRoot, python, onLog }) {
    this.repoRoot = repoRoot
    this.python = python
    this.onLog = onLog || (() => {})
    this.child = null
    this.restarts = 0
    this.stopping = false
    this.startedAt = 0
  }

  start () {
    if (this.child) return
    this.stopping = false
    this.startedAt = Date.now()

    // --foreground 让它在前台阻塞跑主循环，由我们持有进程句柄。
    // 不加这个参数，CLI 只会打印一句「需要 launchd 托管」然后退出。
    this.child = spawn(this.python, ['-m', 'cli', 'daemon', '--foreground'], {
      cwd: this.repoRoot,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
      env: { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' }
    })

    this.child.on('error', (err) => {
      this.onLog(`daemon 启动失败: ${err.message}`)
    })

    const relay = (buf) => {
      const text = String(buf).trim()
      if (text) this.onLog(text)
    }
    this.child.stdout.on('data', relay)
    this.child.stderr.on('data', relay)

    this.child.on('exit', (code, signal) => this.handleExit(code, signal))
  }

  handleExit (code, signal) {
    this.child = null
    if (this.stopping) return

    // 锁被占用说明已经有一个 daemon 在跑（比如用户装了 launchd 服务，
    // 或上一个实例还没退干净）。这不是错误，让位即可，重启只会继续撞锁。
    const ranBriefly = Date.now() - this.startedAt < 3_000
    if (ranBriefly && this.restarts >= 1) {
      this.onLog('定时调度已由其他进程接管，本次不再重试。')
      return
    }

    if (this.restarts >= MAX_RESTARTS) {
      this.onLog(`定时调度多次退出（code=${code} signal=${signal}），已停止重试。`)
      return
    }
    this.restarts += 1
    setTimeout(() => {
      if (!this.stopping) this.start()
    }, RESTART_DELAY_MS)
  }

  stop () {
    this.stopping = true
    const child = this.child
    if (!child) return
    this.child = null
    if (IS_WINDOWS) {
      spawn('taskkill', ['/pid', String(child.pid), '/f', '/t'], { windowsHide: true })
    } else {
      // SIGTERM：daemon 装了信号处理，会释放文件锁后干净退出。
      child.kill('SIGTERM')
    }
  }
}

module.exports = { DaemonRunner }
