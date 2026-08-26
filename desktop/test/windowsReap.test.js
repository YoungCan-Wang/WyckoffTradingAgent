'use strict'

// Windows 上回收进程树用 taskkill，而 spawn 失败是**异步**的 error 事件 ——
// 没人接就是未捕获异常，在主进程里等于整个应用崩掉。
//
// 这条路径只在 Windows 上跑，我们全在 macOS 上开发，所以它崩了我们看不见。
// 触发条件也不罕见：taskkill 不在 PATH（精简版 Windows、PATH 被改坏）、被组策略
// 拦下、或目标进程已经自己退了。
//
// 这里不假装能在 macOS 上跑 Windows —— 测两件能测的事：
// 1. 用真的 spawn 证明「不挂 error 监听会抛未捕获异常」（平台无关的 Node 行为）
// 2. 静态确认三处 taskkill 调用都走了带守卫的那个 helper
const test = require('node:test')
const assert = require('node:assert/strict')
const { spawn } = require('node:child_process')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')

const D = (...p) => join(__dirname, '..', ...p)

test('裸 spawn 一个不存在的命令会抛未捕获异常（这就是崩溃的成因）', async () => {
  // 平台无关：Node 对 ENOENT 一律走 error 事件。这条锁住「为什么必须挂监听」，
  // 而不是锁住我们的代码 —— 它是那个修复存在的理由。
  const err = await new Promise((resolve) => {
    const proc = spawn('definitely-not-a-real-binary-xyz', ['/f'], { windowsHide: true })
    proc.on('error', resolve)
  })
  assert.equal(err.code, 'ENOENT', '命令不存在时应发 ENOENT 的 error 事件')
})

test('带守卫的 helper 不会让 spawn 失败冒泡成未捕获异常', async () => {
  // 直接跑 helper 的实现：把源码里那个函数抠出来，喂一个必然失败的命令。
  const src = readFileSync(D('src', 'python-bridge.js'), 'utf8')
  const fnSrc = src.match(/function killTreeWindows[\s\S]*?\n}/)
  assert.ok(fnSrc, '找不到 killTreeWindows')

  const logs = []
  // 把 taskkill 换成一个不存在的命令，模拟「taskkill 不可用」
  const patched = fnSrc[0].replace("'taskkill'", "'definitely-not-a-real-binary-xyz'")
  const fn = new Function('spawn', `${patched}\nreturn killTreeWindows`)(spawn)

  let uncaught = null
  const onUncaught = (e) => { uncaught = e }
  process.once('uncaughtException', onUncaught)
  fn(999999, (m) => logs.push(m))
  // 给 error 事件一轮事件循环发出来
  await new Promise((r) => setTimeout(r, 300))
  process.removeListener('uncaughtException', onUncaught)

  assert.equal(uncaught, null, `spawn 失败冒泡成未捕获异常了: ${uncaught && uncaught.message}`)
  assert.ok(logs.length > 0, '失败应记一条日志，而不是完全无声')
  assert.match(logs[0], /taskkill/, '日志要说清是回收失败')
})

test('所有 taskkill 调用都走带守卫的 helper', () => {
  for (const file of ['python-bridge.js', 'daemon-runner.js']) {
    const src = readFileSync(D('src', file), 'utf8')
    // helper 自身那一处是唯一允许直接 spawn('taskkill') 的地方
    const direct = [...src.matchAll(/spawn\('taskkill'/g)]
    assert.equal(
      direct.length,
      1,
      `${file} 里有 ${direct.length} 处直接 spawn('taskkill')，应只有 helper 内部那一处`
    )
    assert.match(src, /function killTreeWindows/, `${file} 缺少 killTreeWindows`)
    // helper 内部必须挂 error 监听
    const fn = src.match(/function killTreeWindows[\s\S]*?\n}/)[0]
    assert.match(fn, /\.on\('error'/, `${file} 的 helper 没挂 error 监听`)
  }
})

test('Windows 与 POSIX 两条回收路径都存在，且不互相顶替', () => {
  const bridge = readFileSync(D('src', 'python-bridge.js'), 'utf8')
  const daemon = readFileSync(D('src', 'daemon-runner.js'), 'utf8')
  for (const [name, src] of [['python-bridge', bridge], ['daemon-runner', daemon]]) {
    assert.match(src, /if \(IS_WINDOWS\)/, `${name} 少了平台分支`)
    // POSIX 侧必须有 SIGTERM -> SIGKILL 升级；Windows 的 /f 本身就是强杀
    assert.match(src, /SIGTERM/, `${name} 缺 SIGTERM`)
    assert.match(src, /SIGKILL/, `${name} 缺 SIGKILL 升级`)
    assert.match(src, /killTreeWindows/, `${name} 的 Windows 分支没用 helper`)
  }
})

test('Windows 下的解释器与二进制名走对了平台分支', () => {
  const src = readFileSync(D('src', 'python-bridge.js'), 'utf8')
  // venv 布局：Windows 是 Scripts/python.exe，POSIX 是 bin/python
  assert.match(src, /'Scripts', 'python\.exe'/)
  assert.match(src, /'bin', 'python'/)
  // 打包后的二进制带 .exe
  assert.match(src, /'wyckoff-ipc\.exe'/)
})
