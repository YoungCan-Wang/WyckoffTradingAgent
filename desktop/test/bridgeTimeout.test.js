'use strict'

// 「挂而不死」的请求要能自己收尾。
//
// 原来 pending 只在收到 end 或进程退出时清理。如果某个方法只是挂起（Python 侧
// 卡在一个无限等待的网络调用上）而进程不死，那个 id 就永远留在 pending、事件流
// 永不发 end —— 前端永久转圈。而 Python 侧是 ThreadPoolExecutor(max_workers=4)，
// 几个挂起请求占满线程池之后，所有后续请求静默排队。
//
// 崩溃路径原本处理得很好（failPending 会给每个 pending 补 error+end），唯独缺
// 「进程活着但这一轮不动了」这条。
//
// 关键设计：这是**静默**超时而不是总时长上限。对话和漏斗本来就可能跑十几分钟，
// 按总时长砍会杀掉正常的长任务；每来一个事件就重置计时，只有真的完全没动静才判死。
const test = require('node:test')
const assert = require('node:assert/strict')
const { PythonBridge } = require('../src/python-bridge')

/** 造一个 ready 状态的 bridge，stdin 只记录写入，不真的起进程。 */
function makeBridge () {
  const events = []
  const logs = []
  const written = []
  const bridge = new PythonBridge({
    repoRoot: '/tmp',
    onEvent: (e) => events.push(e),
    onStatus: (s) => logs.push(s)
  })
  bridge.child = { stdin: { writable: true, write: (line) => written.push(line) } }
  bridge.ready = true
  return { bridge, events, logs, written }
}

test('静默超时会补上 error + end，让前端那一轮能收尾', async () => {
  const { bridge, events } = makeBridge()
  // 把窗口缩到几毫秒，避免测试等三分钟
  const original = PythonBridge.IDLE_TIMEOUT_MS
  PythonBridge.IDLE_TIMEOUT_MS = 30
  try {
    const { id } = bridge.send('portfolio', {})
    assert.equal(bridge.pending.size, 1)

    await new Promise((r) => setTimeout(r, 80))

    assert.equal(bridge.pending.size, 0, 'pending 必须被清掉，否则线程池位置一直占着')
    const mine = events.filter((e) => e.id === id)
    assert.equal(mine.length, 2, `应恰好补两个事件，实际 ${JSON.stringify(mine)}`)
    assert.equal(mine[0].type, 'error')
    assert.equal(mine[0].code, 'request_timeout')
    // end 是关键：只发 error 的话前端那一轮永远不结束，UI 继续转圈
    assert.equal(mine[1].type, 'end')
  } finally {
    PythonBridge.IDLE_TIMEOUT_MS = original
  }
})

test('流式请求每来一个事件就续命，不会被误杀', async () => {
  const { bridge, events } = makeBridge()
  const original = PythonBridge.IDLE_TIMEOUT_MS
  PythonBridge.IDLE_TIMEOUT_MS = 60
  try {
    const { id } = bridge.send('chat', { text: 'hi' })

    // 每 25ms 来一个增量，总时长远超 60ms 的窗口
    for (let i = 0; i < 6; i += 1) {
      await new Promise((r) => setTimeout(r, 25))
      bridge.handleLine(JSON.stringify({ id, type: 'delta', text: 'x' }))
    }

    const timedOut = events.some((e) => e.code === 'request_timeout')
    assert.equal(timedOut, false, '一直有事件却被判超时 —— 长对话会被砍断')
    assert.equal(bridge.pending.size, 1, '还在进行中，不该被清掉')

    bridge.handleLine(JSON.stringify({ id, type: 'end' }))
    assert.equal(bridge.pending.size, 0)
  } finally {
    PythonBridge.IDLE_TIMEOUT_MS = original
  }
})

test('收到 end 之后计时器不再开火', async () => {
  const { bridge, events } = makeBridge()
  const original = PythonBridge.IDLE_TIMEOUT_MS
  PythonBridge.IDLE_TIMEOUT_MS = 30
  try {
    const { id } = bridge.send('portfolio', {})
    bridge.handleLine(JSON.stringify({ id, type: 'end' }))
    await new Promise((r) => setTimeout(r, 80))
    // 正常收尾之后又补一条超时错误，会在界面上显示一条假的失败
    assert.equal(events.filter((e) => e.code === 'request_timeout').length, 0)
  } finally {
    PythonBridge.IDLE_TIMEOUT_MS = original
  }
})

test('进程退出清理 pending 时会摘掉计时器', async () => {
  const { bridge, events } = makeBridge()
  const original = PythonBridge.IDLE_TIMEOUT_MS
  PythonBridge.IDLE_TIMEOUT_MS = 30
  try {
    const { id } = bridge.send('portfolio', {})
    bridge.failPending('child_exited', 'Python 进程已退出')

    const afterFail = events.filter((e) => e.id === id).length
    assert.equal(afterFail, 2, 'failPending 应补 error + end')

    await new Promise((r) => setTimeout(r, 80))
    // 计时器没摘的话会给同一个 id 再补一遍，前端会看到重复的结束
    assert.equal(
      events.filter((e) => e.id === id).length,
      afterFail,
      '进程退出后旧计时器又开火了 —— 同一轮被结束两次'
    )
  } finally {
    PythonBridge.IDLE_TIMEOUT_MS = original
  }
})

test('未就绪时不排队、也不留计时器', () => {
  const { bridge } = makeBridge()
  bridge.ready = false
  const res = bridge.send('portfolio', {})
  assert.equal(res.ok, false)
  assert.equal(bridge.pending.size, 0)
})
