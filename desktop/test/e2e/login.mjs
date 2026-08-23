// 登录弹窗：只有起真窗口才验得了的几件事。
//
// 1. 启动直接进工作台（未登录**不**拦路）—— 应用不登录也能用
// 2. 账号行能打开弹窗，弹窗真的居中
// 3. 打开时背景真的 inert，关闭后真的恢复
// 4. Esc 真的关得掉
import { _electron as electron } from 'playwright'
import assert from 'node:assert/strict'
import { mkdtemp } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { tmpdir } from 'node:os'

const APP_DIR = join(dirname(fileURLToPath(import.meta.url)), '..', '..')

let failures = 0
const write = (s = '') => process.stdout.write(`${s}\n`)
const check = (name, fn) => {
  try { fn(); write(`  ok   ${name}`) } catch (err) { failures += 1; write(`  FAIL ${name}: ${err.message}`) }
}

const profile = await mkdtemp(join(tmpdir(), 'wyckoff-login-'))
const home = await mkdtemp(join(tmpdir(), 'wyckoff-login-home-'))
const app = await electron.launch({
  args: [APP_DIR, `--user-data-dir=${profile}`],
  // 干净的家目录 = 未登录。这一份**刻意不设** WYCKOFF_E2E_FAKE_SIGNIN：
  // 要验的正是真实的未登录形态。
  env: { ...process.env, ELECTRON_DISABLE_GPU: '1', HOME: home, USERPROFILE: home }
})
const win = await app.firstWindow()

write('登录弹窗：')

// 未登录也要直接进工作台。`.thread` 而不是 `#side`：窄窗口下侧栏按产品规则收起。
await win.waitForSelector('.thread', { timeout: 30_000 })
const boot = await win.evaluate(() => ({
  workbench: !!document.querySelector('.thread'),
  modal: !!document.querySelector('.login-dlg')
}))
check('未登录也直接进工作台', () => assert.equal(boot.workbench, true))
check('启动时没有拦路的登录界面', () => assert.equal(boot.modal, false))

// 账号行在侧栏里，而窄窗口（CI 约 968px < 1180）下侧栏按产品规则收起 ——
// 必须**先展开再读**。原来在展开之前就读 `.acct-n`，本机宽窗一直是绿的，
// CI 上读到空字符串。
if (await win.locator('.acct').count() === 0) {
  await win.locator('.side-toggle').first().waitFor({ state: 'visible', timeout: 20_000 })
  await win.locator('.side-toggle').first().click()
}
await win.locator('.acct').waitFor({ state: 'visible', timeout: 20_000 })
const acctLabel = await win.evaluate(() => document.querySelector('.acct-n')?.textContent || '')
check('账号行显示未登录', () => assert.match(acctLabel, /未登录|Not signed in/))
await win.locator('.acct').click()
const signin = win.locator('[role="menuitem"]', { hasText: /^登录$|^Sign in$/ })
const hasSignin = await signin.waitFor({ state: 'visible', timeout: 10_000 }).then(() => true, () => false)
check('账号菜单里有登录入口', () => assert.equal(hasSignin, true))

if (hasSignin) {
  await signin.click()
  const opened = await win.locator('.login-dlg').waitFor({ state: 'visible', timeout: 10_000 })
    .then(() => true, () => false)
  check('点登录打开弹窗', () => assert.equal(opened, true))

  if (opened) {
    const state = await win.evaluate(() => {
      const d = document.querySelector('.login-dlg')
      const r = d.getBoundingClientRect()
      return {
        modal: d.getAttribute('aria-modal'),
        centered: Math.abs((r.left + r.width / 2) - window.innerWidth / 2) < 6,
        bgInert: document.querySelector('.thread')?.inert === true,
        focused: document.activeElement?.id || ''
      }
    })
    check('弹窗水平居中', () => assert.equal(state.centered, true))
    check('标为 aria-modal', () => assert.equal(state.modal, 'true'))
    check('打开时背景被冻结', () => assert.equal(state.bgInert, true))
    check('焦点进入弹窗', () => assert.match(state.focused, /^login-/))

    // 空表单要给反馈，且**不依赖后端就绪** —— 字段校验先于 backendReady。
    await win.evaluate(() => {
      document.querySelector('.login-form')
        .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    })
    const err = await win.locator('.login-err').waitFor({ state: 'visible', timeout: 10_000 })
      .then(() => true, () => false)
    check('空表单给出可见错误', () => assert.equal(err, true))

    await win.keyboard.press('Escape')
    const closed = await win.locator('.login-dlg').waitFor({ state: 'detached', timeout: 10_000 })
      .then(() => true, () => false)
    check('Esc 关闭弹窗', () => assert.equal(closed, true))
    // 关闭后背景必须解除 inert，否则整个界面点不动
    const restored = await win.evaluate(() => document.querySelector('.thread')?.inert === false)
    check('关闭后背景恢复可交互', () => assert.equal(restored, true))
  }
}

await app.close()
write(failures ? `\n*** ${failures} 项失败 ***` : '\n全部通过')
process.exit(failures ? 1 : 0)
