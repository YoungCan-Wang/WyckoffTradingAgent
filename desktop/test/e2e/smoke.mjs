// 真的把应用启起来点一遍。
//
// 这份存在的理由：我先前判断「CI 验不了运行时，必须人去 Windows 上点」——
// 那个判断是错的。Playwright 的 _electron.launch 能在 CI 里起真进程、真窗口，
// 拿到渲染端的 DOM。所以那些 Windows 专属分支不必只靠人肉冒烟。
//
// 与其他测试的分工：
// - test/*.test.js 是纯函数与静态形状（不起进程）
// - 这份起真 Electron，验「窗口出来了、页面切得动、菜单点得开」
// - 仍然验不了的：装完之后的安装包行为（NSIS 注册表、开始菜单项）
//
// 刻意不 stub Python：这里要验的是外壳能不能起来。Python 缺失时状态栏会停在
// 「连接中」，那不影响本轮断言 —— 反倒是真实分发场景的下限。
import { _electron as electron } from 'playwright'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const HERE = dirname(fileURLToPath(import.meta.url))
const APP_DIR = join(HERE, '..', '..')

const VIEWS = ['chat', 'tasks', 'approvals', 'portfolio', 'schedules', 'tracking', 'attribution', 'reports']

let failures = 0
const writeLine = (value = '') => process.stdout.write(`${value}\n`)
const check = (name, fn) => {
  try {
    fn()
    writeLine(`  ok   ${name}`)
  } catch (err) {
    failures += 1
    writeLine(`  FAIL ${name}: ${err.message}`)
  }
}

const app = await electron.launch({
  args: [APP_DIR],
  // CI 的 Windows/Linux runner 没有 GPU；不关掉会在启动阶段卡住或刷一堆警告。
  env: { ...process.env, ELECTRON_DISABLE_GPU: '1' }
})

try {
// 渲染端的报错要冒到 CI 日志里 —— 否则白屏在这里也是「测试通过」。
const consoleErrors = []
const win = await app.firstWindow()
win.on('console', (msg) => {
  if (msg.type() !== 'error') return
  const text = msg.text()
  if (/Content-Security-Policy|ERR_CONNECTION|favicon/i.test(text)) return
  consoleErrors.push(text.slice(0, 200))
})
win.on('pageerror', (err) => consoleErrors.push(`pageerror: ${err.message}`))

await win.waitForLoadState('domcontentloaded')

writeLine('冒烟检查：')

// 1) 窗口真的有内容，不是白屏
await win.waitForSelector('.win', { timeout: 20_000 })
const title = await win.title()
check('窗口标题正确', () => assert.match(title, /Wyckoff/))

// 2) 侧栏导航渲染出来了（React 接管的部分）
await win.waitForSelector('.nv', { timeout: 20_000 })
const navCount = await win.locator('.nv').count()
check('侧栏导航完整', () => assert.equal(navCount, VIEWS.length))

// 3) 每个页面都切得过去，且切完有内容
for (const view of VIEWS) {
  const nav = win.locator(`.nv[data-view="${view}"]`)
  if (await nav.count() === 0) {
    check(`页面 ${view} 有入口`, () => assert.fail('导航项不存在'))
    continue
  }
  await nav.click()
  await win.waitForTimeout(200)
  const active = await win.locator('.nv.on').getAttribute('data-view')
  const contentVisible = view === 'chat'
    ? await win.locator('#stream').isVisible()
    : await win.locator('.page #page-body').isVisible()
  check(`页面 ${view} 能打开`, () => {
    assert.equal(active, view)
    assert.equal(contentVisible, true)
  })
}

// 4) 「打开」菜单能弹出 —— 这条曾经因为 openBtn 未定义而整个失效
await win.locator('.nv[data-view="chat"]').click()
await win.waitForTimeout(300)
// 按类名定位，不用 .first() —— 侧栏的账号按钮也是 haspopup=menu，
// 取第一个会点到那个，然后断言「菜单项 >= 3」在只有「设置/退出登录」的
// 账号菜单上失败。（第一版就是这么错的。）
const openBtn = win.locator('.topbtn[aria-haspopup="menu"]')
if (await openBtn.count() > 0) {
  await openBtn.click()
  await win.waitForTimeout(400)
  const items = await win.locator('[role="menuitem"]').count()
  check('打开菜单项完整', () => assert.equal(items, 4))

  // 5) K 线输入浮层 —— 这条正是「点了没反应」那个 bug 的落点
  const kline = win.locator('[role="menuitem"]', { hasText: /K 线|Candle/ })
  if (await kline.count() === 1) {
    await kline.click()
    await win.waitForTimeout(600)
    const box = await win.locator('.symbox').count()
    check('K 线输入浮层出现', () => assert.equal(box, 1, `symbox 数量 ${box}`))
  }
} else {
  check('打开菜单能弹出', () => assert.fail('找不到菜单按钮'))
}

check('渲染端无未预期报错', () =>
  assert.deepEqual(consoleErrors, [], `报错: ${consoleErrors.join(' | ')}`))
} finally {
await app.close()
}

writeLine(failures ? `\n*** ${failures} 项失败 ***` : '\n全部通过')
process.exitCode = failures ? 1 : 0
