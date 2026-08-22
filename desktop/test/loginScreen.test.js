'use strict'

// 登录页：桌面端原来**没有任何登录入口**。
//
// 账号菜单里只有「退出登录」，未登录时点开等于只剩设置 —— 看到「未登录」却
// 无处可登。而用户可能把模型和行情数据源都配在云端，不登录那些配置拉不下来，
// 界面显示「未配置模型」，看起来像应用坏了。
//
// 这份是静态结构检查；「登录页真的接管了工作台」由 e2e 起真窗口验证
// （见 test/e2e/login.mjs）—— 那种断言只有真跑起来才有意义。
const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')

const SRC = (p) => readFileSync(join(__dirname, '..', 'src', 'renderer', p), 'utf8')

test('未登录时登录页取代整个工作台，而不是叠一个弹窗', () => {
  const app = SRC('components/App.tsx')
  // 早返回而不是条件渲染在工作台内部：登录是进入应用的前置条件
  assert.match(app, /if \(!account\.signedIn\) \{[\s\S]*?<LoginScreen/, '未登录应直接返回登录页')
})

test('账号态查明之前什么都不渲染 —— 已登录用户不该闪一下登录页', () => {
  const app = SRC('components/App.tsx')
  assert.match(app, /if \(!account\.checked\) return null/, '缺少 checked 闸门')
  // checked 必须在 loadAccount 里被置真，否则永远白屏
  assert.match(app, /checked: true/, 'loadAccount 没有把 checked 置真')
})

test('登录成功走 loadAccount，不直接 setAccount', () => {
  // loadAccount 还负责清跨账号缓存并广播 account-changed。绕过它会让
  // 上一个账号的持仓留在界面上 —— 那是之前修过的一类问题。
  const app = SRC('components/App.tsx')
  const handler = app.match(/const onSignedIn = useCallback\([\s\S]*?\n  \)/)[0]
  assert.match(handler, /await loadAccount\(\)/, '登录后必须走 loadAccount')
})

test('密码不进任何持久化，成功后立刻从 state 清掉', () => {
  const login = SRC('components/LoginScreen.tsx')
  assert.ok(!/localStorage|sessionStorage/.test(login), '密码不能进浏览器存储')
  assert.match(login, /setPassword\('\'\)/, '成功后应清掉密码 state')
})

test('后端未就绪时禁用提交', () => {
  // 否则点了没反应，看起来像卡死
  const login = SRC('components/LoginScreen.tsx')
  assert.match(login, /disabled=\{busy \|\| !backendReady\}/)
  assert.match(login, /login\.waitingBackend/, '应说明为什么还不能登录')
})

test('区分「密码不对」和「网络不通」', () => {
  // 两者的下一步动作完全不同：统一报「登录失败」会让用户反复试密码，
  // 而其实是网络问题。后端也分了两个 code。
  const login = SRC('components/LoginScreen.tsx')
  assert.match(login, /bad_credentials/)
  assert.match(login, /login\.badCredentials/)
  assert.match(login, /login\.failed/)
})

test('错误用 role=alert，读屏能立刻念出来', () => {
  assert.match(SRC('components/LoginScreen.tsx'), /role="alert"/)
})

test('输入框有关联的 label 与 autoComplete', () => {
  const login = SRC('components/LoginScreen.tsx')
  for (const id of ['login-email', 'login-pw']) {
    assert.match(login, new RegExp(`htmlFor="${id}"`), `${id} 缺少 label`)
  }
  // 让密码管理器能正确填充；autoComplete 缺失时 Safari/Chrome 会乱猜
  assert.match(login, /autoComplete="username"/)
  assert.match(login, /autoComplete="current-password"/)
})

test('两种语言都补齐了登录文案', () => {
  const locales = SRC('locales.js')
  for (const key of [
    'login.title', 'login.email', 'login.password', 'login.submit',
    'login.badCredentials', 'login.failed', 'login.waitingBackend'
  ]) {
    const hits = locales.split(`'${key}'`).length - 1
    assert.equal(hits, 2, `${key} 应在 zh 和 en 各出现一次，实际 ${hits}`)
  }
})

test('登录页预填邮箱但绝不预填密码', () => {
  // 邮箱是「你是谁」，记住它是便利；密码是凭据，预填等于让「退出登录」名不副实。
  const login = SRC('components/LoginScreen.tsx')
  assert.match(login, /useState\(initialEmail\)/, '邮箱应用 initialEmail 初始化')
  assert.match(login, /const \[password, setPassword\] = useState\(''\)/, '密码必须初始为空')
  // 预填后焦点该给密码框 —— 落在已填好的字段上等于逼用户多按一次 Tab
  assert.match(login, /if \(initialEmail\) pwRef\.current\?\.focus\(\)/)
})

test('last_email 来自 account，不是前端自己存的', () => {
  // 存在渲染层（localStorage）会和后端的登录态分叉：退出登录清了后端凭据，
  // 前端却还记着，两边说法不一致。
  const app = SRC('components/App.tsx')
  assert.match(app, /last_email\?: string/, 'account 响应里应有 last_email')
  assert.match(app, /initialEmail=\{account\.lastEmail\}/)
  assert.ok(!/localStorage.*email/i.test(SRC('components/LoginScreen.tsx')), '邮箱不该存在渲染层')
})

test('E2E 旁路在渲染侧，且只认精确的 "1"', () => {
  // 这个旁路最初放在 Python 的 account 方法里 —— 但 CI 上没有 Python payload，
  // 后端起不来，那个分支永远不会被执行。诊断显示界面停在登录页。
  // 判断必须在渲染侧才有效。
  const preload = readFileSync(join(__dirname, '..', 'src', 'preload.js'), 'utf8')
  assert.match(preload, /WYCKOFF_E2E_FAKE_SIGNIN === '1'/, '必须精确比较 "1"')
  const app = SRC('components/App.tsx')
  assert.match(app, /window\.wyckoff\?\.e2eFakeSignin/, 'App 应读渲染侧的标志')
  // 旁路只影响登录态；不能顺手跳过后端状态机或改动登录方法
  assert.ok(!/e2eFakeSignin[\s\S]{0,200}auth_login/.test(app), '旁路不该碰真实登录路径')
})
