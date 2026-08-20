'use strict'

// 定时调度进程的生命周期跟随本应用：应用开着才跑，应用退出就收掉。
// 刻意不装 launchd —— 用户要的是「不开应用就不跑定时任务」。
//
// 和 PythonBridge 分开写：那个是请求/响应通道，需要 ready 握商、pending 表、
// 重启计数；这个是纯后台单向进程，混在一起只会让两种失败模式互相干扰。

const { spawn } = require('node:child_process')
const path = require('node:path')

const IS_WINDOWS = process.platform === 'win32'
// 退出即失败说明环境不对（缺依赖、路径错），重试只是空转。
const MAX_RESTARTS = 3
const RESTART_DELAY_MS = 2_000
// 存活超过这个时长的那次退出，不计入「连续失败」预算。
// 取 5 分钟：足够区分「起来就崩」和「跑了一阵偶发退出」，又不至于让真正的
// 崩溃循环靠慢速崩溃绕过预算。
const HEALTHY_UPTIME_MS = 5 * 60_000
// SIGTERM 之后多久改用 SIGKILL。daemon 要释放文件锁，给的时间比 bridge 宽一点。
const SIGKILL_DELAY_MS = 5_000
const LOCK_BUSY_EXIT_CODE = 75

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

class DaemonRunner {
  constructor ({ repoRoot, python, bundledBinary, onLog }) {
    this.repoRoot = repoRoot
    this.command = bundledBinary || python
    this.args = bundledBinary ? ['--daemon'] : ['-m', 'cli', 'daemon', '--foreground']
    this.cwd = bundledBinary ? path.dirname(bundledBinary) : repoRoot
    this.onLog = onLog || (() => {})
    this.child = null
    this.restarts = 0
    this.stopping = false
    this.startedAt = 0
    this.spawnFailed = false
  }

  start () {
    if (this.child) return
    this.stopping = false
    this.startedAt = Date.now()
    this.spawnFailed = false

    // 开发模式给 CLI 传 --foreground；分发版的 --daemon 入口天然前台阻塞。
    const child = spawn(this.command, this.args, {
      cwd: this.cwd,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
      env: { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' }
    })
    this.child = child

    child.on('error', (err) => {
      if (this.child === child) this.child = null
      this.spawnFailed = true
      this.onLog(`daemon 启动失败: ${err.message}`)
    })

    const relay = (buf) => {
      const text = String(buf).trim()
      if (text) this.onLog(text)
    }
    child.stdout.on('data', relay)
    child.stderr.on('data', relay)

    child.on('exit', (code, signal) => {
      if (this.child !== child) return
      this.handleExit(code, signal)
    })
  }

  handleExit (code, signal) {
    this.child = null
    if (this.stopping) return
    if (this.spawnFailed) return

    // 锁被占用说明已经有一个 daemon 在跑（比如用户装了 launchd 服务，
    // 或上一个实例还没退干净）。这不是错误，让位即可，重启只会继续撞锁。
    const lockBusy = code === LOCK_BUSY_EXIT_CODE
    if (lockBusy) {
      this.onLog('定时调度已由其他进程接管，本次不再重试。')
      return
    }

    // 健康跑够久就把预算还回去。
    //
    // 原来 restarts 只增不减：应用开着几小时、期间偶发退出三次（各自都成功恢复
    // 了），第三次之后就**永久**不再调度，而且 activate 也救不回来 —— 用户看到
    // 的是「定时任务今天以后就不跑了」，没有任何提示。
    //
    // MAX_RESTARTS 想拦的是「起来就崩」的循环，不是「跑了很久偶尔退一次」。
    // 用存活时长区分这两者：撑过 HEALTHY_UPTIME_MS 的那次退出，说明进程本身
    // 是好的，不该计入连续失败。（python-bridge 靠 ready 握手复位，daemon
    // 没有握手，只能看时长。）
    const uptime = this.startedAt ? Date.now() - this.startedAt : 0
    if (uptime >= HEALTHY_UPTIME_MS) {
      this.restarts = 0
    }

    if (this.restarts >= MAX_RESTARTS) {
      this.onLog(`定时调度连续 ${MAX_RESTARTS} 次启动后很快退出（code=${code} signal=${signal}），已停止重试。`)
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
      killTreeWindows(child.pid, this.onLog)
    } else {
      // SIGTERM：daemon 装了信号处理，会释放文件锁后干净退出。
      child.kill('SIGTERM')
      // 但它可能正卡在一轮任务的原生调用里收不到信号。不强杀的话文件锁不释放，
      // 下次启动会撞 LOCK_BUSY 然后「让位」—— 表现为定时任务再也不跑。
      setTimeout(() => {
        if (child.exitCode !== null || child.signalCode !== null) return
        this.onLog('定时调度未响应 SIGTERM，改用 SIGKILL 回收。')
        try {
          child.kill('SIGKILL')
        } catch {
          /* 已经没了 */
        }
      }, SIGKILL_DELAY_MS)
    }
  }
}

module.exports = { DaemonRunner }
