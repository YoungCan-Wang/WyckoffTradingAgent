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

// 这份测的是**工作台**，所以要一个已登录的环境。
//
// 未登录时登录页取代整个工作台（刻意如此：用户可能把模型和数据源都配在云端，
// 不登录界面会显示「未配置模型」）。CI 上没有账号，于是所有侧栏断言都撞在
// 登录页上。
//
// 用环境变量旁路，**不是**伪造 session 文件：假 token 会被 `restore_session`
// 拿去问 Supabase，被判 invalid 就 `clear_session()` —— CI 上稳定回到登录页。
// 那条路我试了三次才查明白（本机不复现：那里的网络错误走「保留 session」分支，
// CI 上则命中「无效凭据」分支）。
//
// 登录本身由 test/e2e/login.mjs 验，那份**不设**这个变量。

// 导航里的页面。**不含 chat** —— 对话是主界面，不是和这些并列的目的地，
// 它的入口是「新建分析」和会话列表。下面单独验它到得了。
const VIEWS = ['approvals', 'portfolio', 'schedules', 'tracking', 'attribution', 'reports']

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
  env: { ...process.env, ELECTRON_DISABLE_GPU: '1', WYCKOFF_E2E_FAKE_SIGNIN: '1' }
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
// 先等界面真的起来，再动 localStorage —— reload 后同样要重新等异步账号检查。
await win.waitForSelector('.win', { timeout: 30_000 })
await win.evaluate(() => localStorage.setItem('wyckoff.sidebar', '0'))
await win.reload({ waitUntil: 'domcontentloaded' })

writeLine('冒烟检查：')

// 1) 窗口真的有内容，不是白屏
//
// 等 `.win` 而不是 `domcontentloaded`：账号态是异步查的，`account.checked` 为假
// 时刻意什么都不渲染（否则已登录用户会先闪一下登录页）。所以 DOM 就绪的那一刻
// 页面还是空的 —— 我加登录闸门后这里三个平台全红，正是因为紧接着就找 `.nv`。
await win.waitForSelector('.win', { timeout: 30_000 })
const title = await win.title()
check('窗口标题正确', () => assert.match(title, /Wyckoff/))

const looseList = await win.evaluate(() => {
  const root = window.WyckoffMd.renderMarkdown('1. 第一项\n\n2. 第二项\n\n3. 第三项')
  return {
    orderedLists: root.querySelectorAll('ol').length,
    items: [...root.querySelectorAll('li')].map((item) => item.textContent)
  }
})
check('Markdown 松散有序列表保持连续', () => {
  assert.equal(looseList.orderedLists, 1)
  assert.deepEqual(looseList.items, ['第一项', '第二项', '第三项'])
})

// CI 的默认窗口小于 1180px，侧栏会按产品规则收起；先从真实入口展开。
//
// 这条是 Windows runner 上真红过一次才补的：侧栏默认是否展开看窗口宽度
// （>= 1180 才开），而窗口宽度取自 `min(1480, 屏幕宽 - 40)`。无头虚拟显示分辨率
// 偏小 → 侧栏收起 → `.nv` 永远等不到。本机 1430px 一直是绿的，所以只有真
// Windows CI 能暴露它 —— 应用本身没问题（收起时切换按钮会移到顶栏），是测试
// 假设了「侧栏总是开着」。
//
// 用 waitFor 而不是立刻 count()：`.win` 出现时 React 才刚开始挂子树，那一刻
// `.side-toggle` 还是 0 个。原来的 count() 查得太早，读到 0 就直接判失败 ——
// 我加登录闸门（账号态异步查完才渲染）之后这个时间差变大，三个平台全红。
if (await win.locator('.nv').count() === 0) {
  const toggle = win.locator('.side-toggle')
  const appeared = await toggle.first()
    .waitFor({ state: 'visible', timeout: 20_000 })
    .then(() => true, () => false)
  if (!appeared) {
    // 失败时把实际 DOM 状态打出来。这条在 CI 上红过三次，每次只报
    // 「false !== true」—— 那不足以判断是登录页拦住了、React 没挂完，
    // 还是窗口尺寸导致侧栏形态不同。**断言失败却不说现场，等于让下一次
    // 排查从零开始。**
    const state = await win.evaluate(() => ({
      loginCard: !!document.querySelector('.login-card'),
      win: !!document.querySelector('.win'),
      shellRoot: !!document.querySelector('.shell-root'),
      side: !!document.getElementById('side'),
      sideToggle: document.querySelectorAll('.side-toggle').length,
      nv: document.querySelectorAll('.nv').length,
      innerWidth: window.innerWidth,
      bodyLen: (document.body.innerHTML || '').length,
      // 顶栏在不在、shell 下挂了什么 —— sideToggle=0 但 shellRoot=true 时，
      // 需要知道是顶栏没渲染还是按钮被条件挡掉了。
      topbar: !!document.querySelector('.top'),
      thread: !!document.querySelector('.thread'),
      shellChildren: [...(document.querySelector('.shell-root')?.children || [])]
        .map((el) => el.className || el.tagName).slice(0, 6),
      icb: document.querySelectorAll('.icb').length
    })).catch((err) => ({ evalFailed: String(err).slice(0, 120) }))
    writeLine(`  诊断: ${JSON.stringify(state)}`)
  }
  check('收起侧栏有展开入口', () => assert.equal(appeared, true))
  if (appeared) await toggle.first().click()
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
  const contentVisible = await win.locator('.page #page-body').isVisible()
  check(`页面 ${view} 能打开`, () => {
    assert.equal(active, view)
    assert.equal(contentVisible, true)
  })
}

// 3b) 对话到得了 —— 它没有导航项，入口是「新建分析」。
await win.locator('.side-new').click()
await win.waitForTimeout(500)
const streamVisible = await win.locator('#stream').isVisible()
check('对话到得了（走新建分析）', () => assert.equal(streamVisible, true))

// 4) 「打开」菜单能弹出 —— 这条曾经因为 openBtn 未定义而整个失效
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

// 会话列表。CI 上没有 Python payload，所以列表会是空的 —— 能断言的是
// 「容器渲染了、固定导航没被它挤掉」，切换交互留给本地手验。
const sessionUi = await win.evaluate(() => ({
  listMounted: !!document.querySelector('[data-testid="session-list"]'),
  navIntact: document.querySelectorAll('.nv').length,
  // 会话区和固定导航必须是两个独立滚动区，否则导航会被会话挤出视野
  navScrolls: getComputedStyle(document.querySelector('.nav')).overflowY,
  sessScrolls: (() => {
    const el = document.querySelector('.sess-scroll')
    return el ? getComputedStyle(el).overflowY : ''
  })()
}))
check('会话列表已挂载且未挤掉固定导航', () => {
  assert.equal(sessionUi.listMounted, true)
  assert.equal(sessionUi.navIntact, VIEWS.length)
  assert.equal(sessionUi.sessScrolls, 'auto')
  assert.notEqual(sessionUi.navScrolls, 'auto', '固定导航不该参与滚动')
})

check('渲染端无未预期报错', () =>
  assert.deepEqual(consoleErrors, [], `报错: ${consoleErrors.join(' | ')}`))
} finally {
  await app.close()
  await rm(PROFILE_DIR, { recursive: true, force: true })
}

writeLine(failures ? `\n*** ${failures} 项失败 ***` : '\n全部通过')
process.exitCode = failures ? 1 : 0
