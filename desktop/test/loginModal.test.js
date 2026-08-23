'use strict'

// 登录弹窗。
//
// 桌面端原来**没有任何登录入口**：账号菜单里只有「退出登录」，未登录时点开
// 等于只剩设置。我第一版把登录做成了全屏页取代工作台 —— 那是过头了：
// 应用不登录也能用（持仓存本地、模型可本地配），登录只是额外接上云端。
// 把可选能力做成进门必答题是错的，所以改成从账号行打开的居中弹窗。
//
// 「弹窗真的居中、背景真的 inert、Esc 真的关」由 e2e 起真窗口验
// （见 test/e2e/login.mjs）—— 那些断言只有真跑起来才有意义。
const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')

const SRC = (p) => readFileSync(join(__dirname, '..', 'src', 'renderer', p), 'utf8')

test('登录是弹窗，不再取代整个工作台', () => {
  const app = SRC('components/App.tsx')
  // 关键：未登录**不能**早返回一个登录页
  assert.ok(!/if \(!account\.signedIn\)[\s\S]{0,80}return/.test(app),
    '未登录不该短路掉整个应用')
  assert.match(app, /<LoginModal/, '应挂载 LoginModal')
  assert.match(app, /open=\{loginOpen\}/, '弹窗由 state 控制开关')
})

test('弹窗复用 .ov/.dlg 骨架而不是自造一套', () => {
  // SettingsModal 那套遮罩、inert、焦点回绕已经在 e2e 里验过；
  // 另写一套只会多一个漏 Tab 的地方。
  const m = SRC('components/LoginModal.tsx')
  assert.match(m, /className="ov"/)
  assert.match(m, /className="dlg login-dlg"/)
  assert.match(m, /aria-modal="true"/)
  assert.match(m, /role="dialog"/)
})

test('打开时冻结背景，关闭时还回去', () => {
  const m = SRC('components/LoginModal.tsx')
  assert.match(m, /node\.inert = open/, '打开要把背景设为 inert')
  assert.match(m, /for \(const node of background\) node\.inert = false/,
    '卸载时必须解除 —— 否则关掉弹窗后整个界面点不动')
})

test('Esc 关闭，Tab 在弹窗内回绕', () => {
  const m = SRC('components/LoginModal.tsx')
  assert.match(m, /e\.key === 'Escape'/)
  // inert 挡住鼠标和背景 Tab 目标，但从最后一个控件继续 Tab 会跳出弹窗
  assert.match(m, /e\.preventDefault\(\); first\.focus\(\)/)
  assert.match(m, /e\.preventDefault\(\); last\.focus\(\)/)
})

test('账号菜单未登录给「登录」，已登录给「退出登录」', () => {
  // 这个菜单原来**只有**退出登录 —— 未登录时点开是条死路。
  const menu = SRC('components/AccountMenu.tsx')
  assert.match(menu, /\{!signedIn \? \(/, '未登录应有登录项')
  assert.match(menu, /menu\.signin/)
  assert.match(menu, /\{signedIn \? \(/, '已登录才有退出项')
  // 登录排在设置之前：它是这个菜单的主要动作
  assert.ok(menu.indexOf("menu.signin") < menu.indexOf("menu.settings"),
    '登录应排在设置之前')
})

test('关闭后清掉密码与错误', () => {
  // 下次打开不该看到上一次的残留（尤其是密码）
  const m = SRC('components/LoginModal.tsx')
  const cleanup = m.match(/if \(open\) return\n[\s\S]*?\}, \[open\]\)/)
  assert.ok(cleanup, '缺少关闭时的清理 effect')
  assert.match(cleanup[0], /setPassword\(''\)/)
  assert.match(cleanup[0], /setError\(''\)/)
})

test('密码不进任何持久化，成功后立刻从 state 清掉', () => {
  const m = SRC('components/LoginModal.tsx')
  assert.ok(!/localStorage|sessionStorage/.test(m), '密码不能进浏览器存储')
  assert.match(m, /setPassword\(''\)/)
})

test('initialEmail 晚到也要生效 —— useState 只读首次挂载值', () => {
  // 实测打包版：后端 3 秒就绪、提示语都变了，邮箱框仍然空的。
  // initialEmail 来自 account（要等 Python 起来），useState 只取挂载那刻的值。
  const m = SRC('components/LoginModal.tsx')
  assert.match(m, /if \(initialEmail && !touched\) setEmail\(initialEmail\)/)
  assert.match(m, /\}, \[initialEmail, touched\]\)/)
})

test('用户动过输入框后不再被外部同步冲掉', () => {
  const m = SRC('components/LoginModal.tsx')
  assert.match(m, /setTouched\(true\); setEmail\(e\.target\.value\)/)
})

test('区分「密码不对」和「网络不通」', () => {
  // 两者下一步动作完全不同：统一报「登录失败」会让用户反复试密码。
  const m = SRC('components/LoginModal.tsx')
  assert.match(m, /bad_credentials/)
  assert.match(m, /login\.badCredentials/)
  assert.match(m, /login\.failed/)
})

test('字段校验在 backendReady 之前', () => {
  // 顺序相反时，后端启动那几秒提交空表单完全没有反馈。
  const m = SRC('components/LoginModal.tsx')
  assert.ok(m.indexOf("login.needBoth") < m.indexOf("if (!backendReady)"),
    '字段校验必须先于 backendReady 检查')
})

test('错误用 role=alert，输入框有 label 与 autoComplete', () => {
  const m = SRC('components/LoginModal.tsx')
  assert.match(m, /role="alert"/)
  for (const id of ['login-email', 'login-pw']) {
    assert.match(m, new RegExp(`htmlFor="${id}"`), `${id} 缺少 label`)
  }
  assert.match(m, /autoComplete="username"/)
  assert.match(m, /autoComplete="current-password"/)
})

test('两种语言都补齐了登录文案', () => {
  const locales = SRC('locales.js')
  for (const key of [
    'login.title', 'login.email', 'login.password', 'login.submit',
    'login.cancel', 'login.badCredentials', 'login.failed', 'menu.signin'
  ]) {
    const hits = locales.split(`'${key}'`).length - 1
    assert.equal(hits, 2, `${key} 应在 zh 和 en 各出现一次，实际 ${hits}`)
  }
})

test('E2E 旁路在渲染侧，且只认精确的 "1"', () => {
  // 放在 Python 的 account 方法里时它永远不会被执行 —— CI 上没有 payload。
  const preload = readFileSync(join(__dirname, '..', 'src', 'preload.js'), 'utf8')
  assert.match(preload, /WYCKOFF_E2E_FAKE_SIGNIN === '1'/)
  assert.match(SRC('components/App.tsx'), /window\.wyckoff\?\.e2eFakeSignin/)
})
