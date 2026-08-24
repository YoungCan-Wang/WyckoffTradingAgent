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
  // 按 effect 的依赖数组切片，而不是按行顺序匹配 —— 后者对空白和语句次序敏感，
  // CI 上因为一处换行差异就红了（本机绿）。
  const m = SRC('components/LoginModal.tsx')
  const effects = m.split('useEffect(')
  const cleanup = effects.find((chunk) => /setPassword\(''\)/.test(chunk) && /\}, \[open\]\)/.test(chunk))
  assert.ok(cleanup, '缺少关闭时清空密码的 effect')
  assert.match(cleanup, /setError\(''\)/, '同一个 effect 里也要清掉错误')
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

test('账号反馈走 toast，不塞进对话流', () => {
  // 对话流是用户与模型的往来记录。「已同步 6 项配置」不属于那段对话，而且
  // sysLine 会让它永久留在记录里 —— 下次翻历史还在，甚至可能进模型上下文。
  const app = SRC('components/App.tsx')
  for (const key of ['login.ok', 'signin.signedOutDone', 'signin.signoutFailed']) {
    const line = app.split('\n').find((l) => l.includes(key))
    assert.ok(line, `找不到 ${key} 的调用`)
    assert.ok(!/sysLine/.test(line), `${key} 仍在往对话流塞：${line.trim()}`)
  }
  assert.match(app, /<Toast items=\{toasts\}/, '应挂载 Toast')
})

test('登录成功一定有反馈，不因为没同步到东西而静默', () => {
  // 原来只在 count > 0 时提示：云端没配过、或本地已经都有时完全静默，
  // 弹窗一关用户不知道成了没有。同步数量是附加信息，不是提示存在的前提。
  const app = SRC('components/App.tsx')
  const idx = app.indexOf('const count = (synced?.models')
  const after = app.slice(idx, idx + 500)
  assert.ok(!/if \(count > 0\)/.test(after), '不该用 count > 0 当提示的前提')
  assert.match(after, /login\.ok/, '总要报「已登录」')
  assert.match(after, /login\.okSynced/, '同步到东西时顺带报数量')
})

test('toast 顶部居中，且不压顶栏分隔线', () => {
  // 账号状态是全局的事：右下角会让它像聊天区的附属提示，视线也容易整条漏掉。
  const css = SRC('app.css')
  const block = css.slice(css.indexOf('.toast-wrap'), css.indexOf('.toast-wrap') + 400)
  assert.match(block, /left: 50%/)
  assert.match(block, /translateX\(-50%\)/)
  assert.ok(!/bottom:/.test(block), '不该再定位在底部')
  // 顶栏底边实测 71px；小于它会横跨分隔线，看着像顶栏的一部分
  const top = Number((block.match(/top: (\d+)px/) || [])[1])
  assert.ok(top > 71, `top=${top} 会压住顶栏分隔线（底边 71px）`)
})

test('toast 自动消失，且错误态抢断朗读', () => {
  const toast = SRC('components/Toast.tsx')
  // 说完就走：不留痕才是这类反馈的正确形态
  assert.match(toast, /setTimeout/, '缺少自动消失')
  assert.match(toast, /onExpire\(item\.id\)/, '到期要从列表摘除')
  // 错误要立刻被念出来（用户会以为已经退出了）；普通提示不打扰
  assert.match(toast, /item\.error \? 'alert' : 'status'/)
  assert.match(toast, /item\.error \? 'assertive' : 'polite'/)
})

test('模型下拉的 fits 类不能写死', () => {
  // 写死 fits 会让 overflow-y:hidden 永远生效 —— 实测 17 个模型时内容 889px
  // 挤进 318px，下面的项直接够不着。必须按实测溢出决定。
  const picker = SRC('components/ModelPicker.tsx')
  assert.ok(!/'mdl-menu up fits'|'mdl-menu fits'/.test(picker),
    'fits 不能硬编码在 className 里')
  assert.match(picker, /node\.scrollHeight <= node\.clientHeight/,
    '应按实测高度判断是否装得下')
  // 不能按 models.length 估：每项两行、长 id 会折行，估算必然偏小
  assert.ok(!/models\.length \* \d+.*setFits|setFits.*models\.length \* \d+/.test(picker),
    '不该用行高估算代替实测')
})

test('后端 ready 后要通知模型选择器重读', () => {
  // ModelPicker 只在挂载时 collect 一次（deps 是 []）。打包后 Python 要几秒才起来，
  // 那一次必然落空，而它没有第二次机会 —— 界面永远停在「未配置模型」，
  // 尽管 ~/.wyckoff/wyckoff.json 里有 17 个模型、后端也确实返回了 17 个。
  // 开发环境后端已在运行，所以这个缺口一直没露出来。
  const app = SRC('components/App.tsx')
  const applyStart = app.indexOf('const apply = (state: string)')
  const applyEnd = app.indexOf('wasReady = true', applyStart)
  assert.ok(applyStart > 0 && applyEnd > applyStart, '找不到 backend status 的 apply()')
  const body = app.slice(applyStart, applyEnd)
  assert.match(body, /wyckoff:models-changed/,
    'ready 时必须广播 models-changed，否则打包版模型列表永远为空')

  // ModelPicker 那一侧要真的在听
  const picker = SRC('components/ModelPicker.tsx')
  assert.match(picker, /addEventListener\('wyckoff:models-changed'/)
})
