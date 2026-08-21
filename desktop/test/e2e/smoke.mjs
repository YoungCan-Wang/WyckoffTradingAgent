// 启动真实 Electron 进程验证渲染外壳。安装器和打包后的 Python payload
// 仍由 Windows 安装包冒烟覆盖。
import { _electron as electron } from 'playwright'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { mkdtemp, rm } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { tmpdir } from 'node:os'

const HERE = dirname(fileURLToPath(import.meta.url))
const APP_DIR = join(HERE, '..', '..')
const PROFILE_DIR = await mkdtemp(join(tmpdir(), 'wyckoff-e2e-'))

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
  args: [APP_DIR, `--user-data-dir=${PROFILE_DIR}`],
  // CI 的 Windows/Linux runner 没有 GPU；不关掉会在启动阶段卡住或刷一堆警告。
  env: { ...process.env, ELECTRON_DISABLE_GPU: '1' }
})

try {
// 渲染端的报错要冒到 CI 日志里 —— 否则白屏在这里也是「测试通过」。
const consoleErrors = []
const win = await app.firstWindow()
win.on('console', (msg) => {
  if (msg.type() !== 'error' && !/MaxListenersExceededWarning/.test(msg.text())) return
  const text = msg.text()
  if (/Content-Security-Policy|ERR_CONNECTION|favicon/i.test(text)) return
  consoleErrors.push(text.slice(0, 200))
})
win.on('pageerror', (err) => consoleErrors.push(`pageerror: ${err.message}`))

await win.waitForLoadState('domcontentloaded')
await win.evaluate(() => localStorage.setItem('wyckoff.sidebar', '0'))
await win.reload({ waitUntil: 'domcontentloaded' })

writeLine('冒烟检查：')

// 1) 窗口真的有内容，不是白屏
await win.waitForSelector('.win', { timeout: 20_000 })
const title = await win.title()
check('窗口标题正确', () => assert.match(title, /Wyckoff/))

// CI 的默认窗口小于 1180px，侧栏会按产品规则收起；先从真实入口展开。
//
// 这条是 Windows runner 上真红过一次才补的：侧栏默认是否展开看窗口宽度
// （>= 1180 才开），而窗口宽度取自 `min(1480, 屏幕宽 - 40)`。无头虚拟显示分辨率
// 偏小 → 侧栏收起 → `.nv` 永远等不到。本机 1430px 一直是绿的，所以只有真
// Windows CI 能暴露它 —— 应用本身没问题（收起时切换按钮会移到顶栏），是测试
// 假设了「侧栏总是开着」。
if (await win.locator('.nv').count() === 0) {
  const toggle = win.locator('.side-toggle')
  const toggleCount = await toggle.count()
  check('收起侧栏有展开入口', () => assert.equal(toggleCount, 1))
  if (toggleCount === 1) await toggle.click()
}
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
// 账号按钮也有 aria-haspopup="menu"，所以用顶栏按钮类名消除歧义。
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
    await win.keyboard.press('Escape')
  }
} else {
  check('打开菜单能弹出', () => assert.fail('找不到菜单按钮'))
}

// 6) 设置弹窗只冻结背景，不能把自己也变成 inert；关闭后焦点回到账号按钮。
await win.locator('.acct').click()
await win.locator('.menu-i').first().click()
await win.locator('.dlg').waitFor({ state: 'visible' })
const modalFocused = await win.waitForFunction(
  () => document.activeElement?.classList.contains('dlg-n'),
  undefined,
  { timeout: 5_000 }
).then(() => true, () => false)
const modalState = await win.evaluate(() => ({
  dialogInert: document.querySelector('.dlg')?.inert,
  backgroundInert: document.querySelector('.thread')?.inert
}))
check('设置弹窗可聚焦且只冻结背景', () => {
  assert.equal(modalState.dialogInert, false)
  assert.equal(modalState.backgroundInert, true)
  assert.equal(modalFocused, true)
})
await win.keyboard.press('Escape')
await win.locator('.dlg').waitFor({ state: 'hidden' })
const restored = await win.waitForFunction(
  () => document.activeElement?.classList.contains('acct'),
  undefined,
  { timeout: 5_000 }
).then(() => true, () => false)
check('关闭设置后焦点回到账号按钮', () => assert.equal(restored, true))

check('渲染端无未预期报错', () =>
  assert.deepEqual(consoleErrors, [], `报错: ${consoleErrors.join(' | ')}`))
} finally {
  await app.close()
  await rm(PROFILE_DIR, { recursive: true, force: true })
}

writeLine(failures ? `\n*** ${failures} 项失败 ***` : '\n全部通过')
process.exitCode = failures ? 1 : 0
