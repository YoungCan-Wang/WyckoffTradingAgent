// 登录闸门：只有起真窗口才能验的两件事。
//
// 1. 未登录时登录页**取代**工作台（不是叠一层）
// 2. 已登录时**不闪**登录页 —— 那需要真实的异步时序，静态断言测不出来
import { _electron as electron } from 'playwright'
import assert from 'node:assert/strict'
import { mkdtemp, writeFile, mkdir } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { tmpdir } from 'node:os'

const APP_DIR = join(dirname(fileURLToPath(import.meta.url)), '..', '..')

let failures = 0
const write = (s = '') => process.stdout.write(`${s}\n`)
const check = (name, fn) => {
  try { fn(); write(`  ok   ${name}`) } catch (err) { failures += 1; write(`  FAIL ${name}: ${err.message}`) }
}

async function launch (home) {
  const profile = await mkdtemp(join(tmpdir(), 'wyckoff-login-'))
  return electron.launch({
    args: [APP_DIR, `--user-data-dir=${profile}`],
    env: { ...process.env, ELECTRON_DISABLE_GPU: '1', ...(home ? { HOME: home } : {}) }
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
  await win.waitForTimeout(300)
  check('空表单给出可见错误', async () =>
    assert.equal(await win.evaluate(() => !!document.querySelector('.login-err')), true))
  await app.close()
}

// --- 已登录：不闪登录页 ---
{
  const home = await mkdtemp(join(tmpdir(), 'wyckoff-home2-'))
  await mkdir(join(home, '.wyckoff'), { recursive: true })
  // 伪造一个 session：account 方法只看 access_token 是否存在
  await writeFile(
    join(home, '.wyckoff', 'session.json'),
    JSON.stringify({ user_id: 'e2e-user', email: 'e2e@example.com', access_token: 'fake', refresh_token: 'fake' }),
    'utf8'
  )
  const app = await launch(home)
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
  check('已登录时工作台出现', async () =>
    assert.equal(await win.evaluate(() => !!document.getElementById('side')), true))
  check('已登录时从未闪过登录页', () => assert.equal(sawLogin, false))
  await app.close()
}

write(failures ? `\n*** ${failures} 项失败 ***` : '\n全部通过')
process.exit(failures ? 1 : 0)
