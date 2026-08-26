'use strict'

// 定时调度的重启预算要按「是否健康跑过」复位，不能只增不减。
//
// 原来 restarts 只增不减：应用开着几小时、期间偶发退出三次（每次都成功恢复了），
// 第三次之后就**永久**不再调度，activate 也救不回来。用户看到的是「定时任务今天
// 以后就不跑了」，没有任何提示。
//
// MAX_RESTARTS 想拦的是「起来就崩」的循环，不是「跑很久偶尔退一次」。用存活时长
// 区分：撑过 HEALTHY_UPTIME_MS 的那次退出不计入连续失败。
// （python-bridge 靠 ready 握手复位，daemon 没有握手，只能看时长。）
const test = require('node:test')
const assert = require('node:assert/strict')
const { DaemonRunner } = require('../src/daemon-runner')

/** 不真的起进程：只观察 handleExit 之后有没有安排下一次 start。 */
function makeRunner () {
  const logs = []
  const runner = new DaemonRunner({
    repoRoot: '/tmp',
    python: '/usr/bin/true',
    onLog: (m) => logs.push(m)
  })
  let starts = 0
  runner.start = () => { starts += 1 }
  return { runner, logs, startCount: () => starts }
}

test('连续快速退出用尽预算后停止重试', async () => {
  const { runner, logs } = makeRunner()
  for (let i = 0; i < 5; i += 1) {
    runner.startedAt = Date.now() // 刚起就退
    runner.handleExit(1, null)
  }
  assert.ok(runner.restarts <= 3, `restarts 应停在预算上限，实际 ${runner.restarts}`)
  assert.ok(
    logs.some((m) => m.includes('已停止重试')),
    '用尽预算要说一声，否则用户不知道定时任务停了'
  )
})

test('健康跑过之后那次退出把预算还回去', () => {
  const { runner } = makeRunner()

  // 先攒两次快速失败
  runner.startedAt = Date.now()
  runner.handleExit(1, null)
  runner.startedAt = Date.now()
  runner.handleExit(1, null)
  assert.equal(runner.restarts, 2)

  // 这一次跑了很久才退 —— 进程本身是好的
  runner.startedAt = Date.now() - 10 * 60_000
  runner.handleExit(1, null)

  // 复位后重新计数，所以是 1 而不是 3
  assert.equal(runner.restarts, 1, '健康运行之后预算必须复位')
})

test('长期运行里偶发退出永远不会耗尽预算', () => {
  const { runner, startCount } = makeRunner()
  // 模拟开一整天：每次都健康跑很久然后退一次
  for (let i = 0; i < 20; i += 1) {
    runner.startedAt = Date.now() - 30 * 60_000
    runner.handleExit(1, null)
  }
  assert.ok(runner.restarts < 3, `偶发退出把预算耗尽了，restarts=${runner.restarts}`)
  assert.equal(startCount(), 0, 'start 是延迟调度的，这里不该同步调用')
})

test('锁被占用时让位，且不消耗预算', () => {
  const { runner, logs } = makeRunner()
  runner.startedAt = Date.now()
  runner.handleExit(75, null) // LOCK_BUSY_EXIT_CODE
  assert.equal(runner.restarts, 0, '让位不是失败，不该计入预算')
  assert.ok(logs.some((m) => m.includes('其他进程接管')))
})

test('主动 stop 之后不再重启', () => {
  const { runner } = makeRunner()
  runner.stopping = true
  runner.handleExit(0, 'SIGTERM')
  assert.equal(runner.restarts, 0)
})
