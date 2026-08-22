// 登录闸门：只有起真窗口才能验的两件事。
//
// 1. 未登录时登录页**取代**工作台（不是叠一层）
// 2. 已登录时**不闪**登录页 —— 那需要真实的异步时序，静态断言测不出来
import { _electron as electron } from 'playwright'
import assert from 'node:assert/strict'
import { mkdtemp, mkdir } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { tmpdir } from 'node:os'

const APP_DIR = join(dirname(fileURLToPath(import.meta.url)), '..', '..')

let failures = 0
const write = (s = '') => process.stdout.write(`${s}\n`)
const check = (name, fn) => {
  try { fn(); write(`  ok   ${name}`) } catch (err) { failures += 1; write(`  FAIL ${name}: ${err.message}`) }
}

async function launch (home, signedIn = false) {
  const profile = await mkdtemp(join(tmpdir(), 'wyckoff-login-'))
  return electron.launch({
    args: [APP_DIR, `--user-data-dir=${profile}`],
    env: {
      ...process.env,
      ELECTRON_DISABLE_GPU: '1',
      ...(home ? { HOME: home, USERPROFILE: home } : {}),
      ...(signedIn ? { WYCKOFF_E2E_FAKE_SIGNIN: '1' } : {})
    }
  })
}

write('登录闸门：')

// --- 未登录：登录页接管 ---
{
  const home = await mkdtemp(join(tmpdir(), 'wyckoff-home-'))
  const app = await launch(home)
  const win = await app.firstWindow()
  await win.waitForSelector('.login-card', { timeout: 25000 })
  const state = await win.evaluate(() => ({
    login: !!document.querySelector('.login-card'),
    sidebar: !!document.getElementById('side'),
    focused: document.activeElement?.id || '',
    disabled: !!document.querySelector('.login-go')?.disabled
  }))
  check('未登录时显示登录页', () => assert.equal(state.login, true))
  check('工作台不同时存在（取代，不是叠层）', () => assert.equal(state.sidebar, false))
  check('自动聚焦邮箱输入框', () => assert.equal(state.focused, 'login-email'))

  // 空表单不该静默
  await win.evaluate(() => {
    const form = document.querySelector('.login-card')
    form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
  })
  // 等条件而不是等固定时长：300ms 在本机够（偶发失败过一次），在 CI runner
  // 上稳定不够 —— 提交表单要过一轮 React 状态更新。固定 sleep 的测试
  // 「本机绿、CI 红」，而调大数字只是把不确定性推远一点。
  const hasError = await win.locator('.login-err')
    .waitFor({ state: 'visible', timeout: 10_000 })
    .then(() => true, () => false)
  // 先 await 出结果再进同步的 check：`check` 不会 await 回调，传 async 函数
  // 会让断言的 Promise 逃出它的 try/catch —— 失败变成未捕获拒绝，进程带着
  // 「全部通过」的输出非零退出（实测遇到过一次这种自相矛盾的结果）。
  if (!hasError) {
    const st = await win.evaluate(() => ({
      loginCard: !!document.querySelector('.login-card'),
      errEl: !!document.querySelector('.login-err'),
      emailVal: document.getElementById('login-email')?.value ?? null,
      pwVal: (document.getElementById('login-pw')?.value ?? '').length,
      submitDisabled: document.querySelector('.login-go')?.disabled,
      hintText: document.querySelector('.login-hint')?.textContent?.slice(0, 40)
    })).catch((e) => ({ evalFailed: String(e).slice(0, 100) }))
    write(`  诊断(空表单): ${JSON.stringify(st)}`)
  }
  check('空表单给出可见错误', () => assert.equal(hasError, true))
  await app.close()
}

// --- 已登录：不闪登录页 ---
{
  const home = await mkdtemp(join(tmpdir(), 'wyckoff-home2-'))
  await mkdir(join(home, '.wyckoff'), { recursive: true })
  // 伪造 session 文件在 CI 上不可靠：`restore_session` 会拿假 token 去问
  // Supabase，被判 invalid 就 clear_session，于是「已登录」这一半永远测不到。
  // 所以这里改用 smoke 同一个环境变量旁路。**上半段（未登录）刻意不设它**，
  // 那才是真实的登录闸门。
  const app = await launch(home, true)
  const win = await app.firstWindow()
  // 一进来就轮询：如果登录页曾经出现过，这里能抓到
  let sawLogin = false
  const started = Date.now()
  while (Date.now() - started < 20000) {
    const s = await win.evaluate(() => ({
      login: !!document.querySelector('.login-card'),
      sidebar: !!document.getElementById('side')
    })).catch(() => null)
    if (s?.login) sawLogin = true
    if (s?.sidebar) break
    await win.waitForTimeout(120)
  }
  const workbench = await win.evaluate(() => !!document.getElementById('side'))
  if (!workbench) {
    const st = await win.evaluate(() => ({
      loginCard: !!document.querySelector('.login-card'),
      shellChildren: [...(document.querySelector('.shell-root')?.children || [])]
        .map((el) => el.className || el.tagName).slice(0, 4),
      bypass: !!window.wyckoff?.e2eFakeSignin,
      innerWidth: window.innerWidth
    })).catch((e) => ({ evalFailed: String(e).slice(0, 100) }))
    write(`  诊断(已登录): ${JSON.stringify(st)}`)
  }
  check('已登录时工作台出现', () => assert.equal(workbench, true))
  check('已登录时从未闪过登录页', () => assert.equal(sawLogin, false))
  await app.close()
}

write(failures ? `\n*** ${failures} 项失败 ***` : '\n全部通过')
process.exit(failures ? 1 : 0)
