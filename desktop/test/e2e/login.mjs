// 登录弹窗：只有起真窗口才验得了的几件事。
//
// 1. 启动直接进工作台（未登录**不**拦路）—— 应用不登录也能用
// 2. 账号行能打开弹窗，弹窗真的居中
// 3. 打开时背景真的 inert，关闭后真的恢复
// 4. Esc 真的关得掉
import assert from 'node:assert/strict'
import { launchApp, reporter } from './harness.mjs'

// 不要把 reporter 解构开 —— failures 是 getter，解构会把它拍成快照（永远 0）。
const r = reporter()
const { write, check } = r

// 干净的家目录 = 未登录。这一份**刻意 signedIn: false**：要验的正是真实的
// 未登录形态，所以它不能和 workbench.mjs 共用窗口。
const { app } = await launchApp({ signedIn: false, tag: 'login' })
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
    // 等焦点真的落进弹窗再读 activeElement。
    //
    // 这是一个**改动前就存在**的 flake，本机 5 轮里红 4 轮：`waitFor visible` 只
    // 保证元素可见，而自动聚焦是 React 挂载后的一个 effect，晚一帧才跑。原来
    // 紧接着就 evaluate，读到的 activeElement 常常还是 body（空字符串），
    // 报成「焦点进入弹窗失败」。用 waitForFunction 轮询到位再断言。
    const focusArrived = await win.waitForFunction(
      () => /^login-/.test(document.activeElement?.id || ''),
      undefined,
      { timeout: 5_000 }
    ).then(() => true, () => false)

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
    check('焦点进入弹窗', () =>
      assert.ok(focusArrived, `5s 内焦点没进弹窗，activeElement=${state.focused || '(空)'}`))

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
r.finish()
