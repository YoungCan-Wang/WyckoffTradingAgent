'use strict'

// 升级 Electron 大版本时最容易出的事：某个 API 的签名或行为变了，代码还在用旧的
// 那套，而且**不报错**——只是静默失效。这组测试锁住我们依赖的那几处契约。
//
// 全部是静态检查（读源码 + package.json），因为真正的运行期验证需要起 Electron；
// 那部分在升级时手工跑过探针，这里保证的是「代码没有退回旧写法」。
const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')

const D = (...p) => join(__dirname, '..', ...p)
const pkg = JSON.parse(readFileSync(D('package.json'), 'utf8'))
const mainSrc = readFileSync(D('src', 'main.js'), 'utf8')
const hostSrc = readFileSync(D('src', 'browser-host.js'), 'utf8')

test('Electron 版本仍在受支持范围内', () => {
  const pinned = String(pkg.devDependencies.electron || '')
  const major = Number(pinned.replace(/^[^0-9]*/, '').split('.')[0])
  assert.ok(Number.isFinite(major), `版本号解析不出来: ${pinned}`)
  // 官方策略：最新三个大版本受支持，每 8 周一个大版本。
  // 41 在 2026-08 到期、42 在 2026-10、43 在 2027-01。低于 41 就是在跑没有安全
  // 补丁的运行时 —— 这个应用会加载模型生成的 HTML，还带一个内置浏览器。
  assert.ok(major >= 41, `Electron ${major} 已 EOL 或即将到期，必须升级`)
})

test('版本号是精确固定的，不带 ^ 或 ~', () => {
  // Electron 的补丁版本里会带 Chromium 安全修复，也偶尔带行为变化。
  // 用范围号意味着「谁装的时候是什么就是什么」，两台机器可能跑不同的运行时。
  const pinned = String(pkg.devDependencies.electron || '')
  assert.match(pinned, /^\d+\.\d+\.\d+$/, `应精确固定，实际 ${pinned}`)
})

test('console-message 用 Event 对象签名，不用废弃的位置参数', () => {
  // 位置参数那套（level 是数字）自 35.0 起废弃。两套目前都还发，所以写错了
  // **不会报错**，只是等兼容层摘掉之后 `level >= 2` 恒为 false ——
  // 渲染端错误日志是诊断白屏的唯一手段，静默丢掉最糟。
  const handler = mainSrc.match(/on\('console-message',[\s\S]{0,400}?\n  \}\)/)
  assert.ok(handler, '找不到 console-message 处理器')
  const body = handler[0]
  assert.ok(
    !/\(_e,\s*level\s*,/.test(body),
    '还在用 (_e, level, message, line, source) 位置参数'
  )
  assert.ok(!/level\s*>=\s*\d/.test(body), 'level 现在是字符串，数字比较恒为 false')
  // Event 上的 level 取值：info | warning | error | debug（没有 verbose）
  assert.ok(
    /'warning'/.test(body) && /'error'/.test(body),
    '应按字符串等级筛选 warning / error'
  )
})

test('导出 PDF 自己记住上次目录', () => {
  // Electron 43 起：defaultPath 不含目录时一律落到「下载」，且操作系统**不再**
  // 跨次记住上次用的目录。导出多份报告到同一个文件夹是常态。
  assert.match(mainSrc, /lastExportDir/, '没有记住上次导出目录')
  assert.match(mainSrc, /lastExportDir = path\.dirname\(/, '选完之后要把目录记下来')
})

test('浏览器动作在没有页面时立刻失败，而不是等超时', () => {
  // 从未导航过的 WebContentsView 上 executeJavaScript 永不 resolve（43 实测）。
  // withTimeout 能兜住，但那是白等 15 秒才报「script timed out」——
  // 而「还没有页面」当下就知道。
  assert.match(hostSrc, /if \(!wc\.getURL\(\)\)/, '缺少「还没有页面」的前置检查')
  assert.match(hostSrc, /navigate first/, '错误信息要告诉调用方该先导航')
})

test('仍用现代的 WebContentsView / contentView，而不是 BrowserView', () => {
  // BrowserView 在 30.0 就废弃了。
  assert.match(hostSrc, /WebContentsView/)
  assert.ok(!/new BrowserView\(/.test(hostSrc), 'BrowserView 已废弃')
  assert.match(hostSrc, /contentView\.(addChildView|removeChildView)/)
})

test('仍用 navigationHistory 而不是扁平的 canGoBack', () => {
  // webContents.canGoBack() 在 32.0 废弃。
  assert.match(hostSrc, /navigationHistory\.canGoBack\(\)/)
  assert.ok(
    !/wc\.canGoBack\(\)/.test(hostSrc),
    '扁平 canGoBack 已废弃，用 navigationHistory'
  )
})

test('SSRF 校验必须拿到 session，不能退回 Node 的 dns.lookup', () => {
  const urlSrc = readFileSync(D('src', 'public-url.js'), 'utf8')
  assert.ok(
    !/require\('node:dns'\)/.test(urlSrc),
    '用 Node 解析等于「校验和连接各解析一次」，DNS rebinding 可绕过'
  )
  assert.match(urlSrc, /session\.resolveHost/, '必须用发起请求那个 session 解析')
  // 返回形状 { endpoints: [{ address, family }] }
  assert.match(urlSrc, /endpoints/, 'resolveHost 的返回形状是 { endpoints: [...] }')
})

test('渲染进程的安全开关没被放松', () => {
  for (const flag of ['contextIsolation: true', 'nodeIntegration: false', 'sandbox: true']) {
    assert.ok(mainSrc.includes(flag), `主窗口缺少 ${flag}`)
    assert.ok(hostSrc.includes(flag), `浏览器视图缺少 ${flag}`)
  }
  // 浏览器视图绝不能有 preload —— 那是通往应用的桥。
  // 注意要匹配**代码**而不是注释：那里恰好有一句「无 preload：…」的说明。
  const viewPrefs = hostSrc.match(/new WebContentsView\(\{[\s\S]*?\n    \}\)/)
  assert.ok(viewPrefs, '找不到 WebContentsView 的构造')
  const codeOnly = viewPrefs[0]
    .split('\n')
    .filter((line) => !line.trim().startsWith('//'))
    .join('\n')
  assert.ok(!/preload\s*:/.test(codeOnly), '内置浏览器视图不该有 preload')
})

test('审批理由白名单与后端保持同步', () => {
  // 白名单挡的是「别把内部代号漏给用户」，代价是后端新增理由、前端忘了加，
  // 那条理由就**静默消失**。升级时发现的真实漏洞：clears_stop_loss 在后端和
  // 译文里都有了，白名单里没有，于是「这会移除止损保护」根本没显示出来 ——
  // 而那是最需要让用户看到的一句。
  const policy = readFileSync(
    join(__dirname, '..', '..', 'cli', 'approval_policy.py'),
    'utf8'
  )
  const backend = new Set(
    [...policy.matchAll(/return "reason\.([a-z_]+)"/g)].map((m) => m[1])
  )
  assert.ok(backend.size > 0, '没从 approval_policy.py 里解析到任何理由')

  const front = readFileSync(
    join(__dirname, '..', 'src', 'renderer', 'lib', 'schedules.ts'),
    'utf8'
  )
  const block = front.match(/const KNOWN_REASONS = new Set\(\[[\s\S]*?\]\)/)
  assert.ok(block, '找不到 KNOWN_REASONS')

  const missing = [...backend].filter((name) => !block[0].includes(`'${name}'`))
  assert.deepEqual(missing, [], `后端有这些理由但前端白名单里没有（会静默不显示）: ${missing}`)

  // 译文也得有，中英都要
  const locales = readFileSync(
    join(__dirname, '..', 'src', 'renderer', 'locales.js'),
    'utf8'
  )
  for (const name of backend) {
    const hits = locales.split(`'approvals.reason.${name}'`).length - 1
    assert.equal(hits, 2, `approvals.reason.${name} 应中英各一条，实际 ${hits} 条`)
  }
})

test('Windows 目标同时覆盖 x64 与 arm64', () => {
  // Apple Silicon 上的 Windows 虚拟机一律是 ARM 版。只出 x64 的话本地验证只能
  // 过 Prism 模拟跑 —— 测出来的性能和部分原生行为不代表真实 x64 机器。
  // （NSIS 会把两个架构合进同一个安装包，所以产物里没有单独的 *arm64*.exe。）
  const win = pkg.build && pkg.build.win
  assert.ok(win, '缺 build.win')
  const arches = new Set()
  for (const target of win.target || []) {
    if (typeof target === 'string') continue
    for (const a of target.arch || []) arches.add(a)
  }
  assert.ok(arches.has('x64'), 'Windows 目标缺 x64')
  assert.ok(arches.has('arm64'), 'Windows 目标缺 arm64（虚拟机验证会退化成模拟运行）')
})
