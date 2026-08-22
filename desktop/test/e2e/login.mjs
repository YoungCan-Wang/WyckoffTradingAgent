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
    sidebar: !!document.querySelector('.thread'),
    focused: document.activeElement?.id || '',
    disabled: !!document.querySelector('.login-go')?.disabled
  }))
  check('未登录时显示登录页', () => assert.equal(state.login, true))
  check('工作台不同时存在（取代，不是叠层）', () => assert.equal(state.sidebar, false))
  check('自动聚焦邮箱输入框', () => assert.equal(state.focused, 'login-email'))

  // 空表单不该静默 —— 而且**不该依赖后端就绪**。
  //
  // CI 上没有 Python payload，后端永远不会 ready（等 60 秒也不行）。原来的
  // `submit()` 先查 backendReady 再校验字段，所以那时提交空表单没有任何反馈。
  // 那本身是个 UX 缺陷（后端启动的几秒正是用户第一次尝试的时刻），已修好：
  // 字段校验现在在前面。这条测试因此也能在无后端的环境里跑。
  await win.evaluate(() => {
    const form = document.querySelector('.login-card')
    form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
  })
  const hasError = await win.locator('.login-err')
    .waitFor({ state: 'visible', timeout: 10_000 })
    .then(() => true, () => false)
  if (!hasError) {
    const st = await win.evaluate(() => ({
      loginCard: !!document.querySelector('.login-card'),
      errEl: !!document.querySelector('.login-err'),
      emailVal: document.getElementById('login-email')?.value ?? null,
      submitDisabled: document.querySelector('.login-go')?.disabled
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
      sidebar: !!document.querySelector('.thread')
    })).catch(() => null)
    if (s?.login) sawLogin = true
    if (s?.sidebar) break
    await win.waitForTimeout(120)
  }
  // 判据用 `.thread` 而不是 `#side`：CI 诊断显示 shellChildren 是
  // ["sr-only","thread"] —— 窄窗口（968px < 1180）下侧栏按产品规则收起，
  // `#side` 压根不渲染。工作台是否出现应该看主区，不是看侧栏。
  const workbench = await win.evaluate(() => !!document.querySelector('.thread'))
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
