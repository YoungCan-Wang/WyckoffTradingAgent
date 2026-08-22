'use strict'

// 产物建模与自动展开决策。
//
// 这两个模块是纯函数,所以能直接跑 —— 而「什么时候自动展开」原来散在
// useChat 的 effect 里,只能靠手点验证。抽出来之后六条规则每条都能锁住。
const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const ts = require('typescript')

const R = (...p) => join(__dirname, '..', 'src', 'renderer', ...p)

/** 编译一个 .ts 模块并取出导出（测试环境没有打包器）。 */
function load (rel) {
  const src = readFileSync(R('lib', rel), 'utf8')
  const js = ts.transpileModule(src, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
  }).outputText
  const mod = { exports: {} }
  new Function('module', 'exports', 'require', js)(mod, mod.exports, require)
  return mod.exports
}

const { parseArtifactEvent, mergeArtifact, reportArtifact } = load('artifacts.ts')
const { decideAutoOpen, resetForTurn, MIN_SPLIT_WIDTH } = load('autoOpen.ts')

const READY = { id: 't1:c1', kind: 'kline', title: '600519', status: 'ready', payload: {} }
const IDLE = { openedThisTurn: false, dismissedThisTurn: false, viewing: null, width: 1400 }

test('解析产物事件读的是 artifact_id,不是 id', () => {
  // 传输层会把 event.id 覆盖成请求流 id;读 id 会拿到错的东西
  const a = parseArtifactEvent({
    type: 'chat_artifact', artifact_id: 't1:c1', kind: 'kline', title: '600519',
    status: 'ready', payload: { symbol: '600519' }, id: 'stream-9'
  })
  assert.equal(a.id, 't1:c1', '应该用 artifact_id')
})

test('不是产物事件就返回 null,不抛错', () => {
  // 事件来自另一个进程,版本不一致时少一张卡片好过整个对话流挂掉
  for (const bad of [
    { type: 'text_delta', text: 'x' },
    { type: 'chat_artifact' },
    { type: 'chat_artifact', artifact_id: 'x', kind: 'unknown-kind' },
    {}
  ]) {
    assert.doesNotThrow(() => parseArtifactEvent(bad))
    assert.equal(parseArtifactEvent(bad), null)
  }
})

test('合并按 id 去重,且保持首次出现的位置', () => {
  // 重画一张图不该让它跳到列表最后
  const first = { ...READY, id: 'a' }
  const second = { ...READY, id: 'b', title: '000001' }
  let list = mergeArtifact(mergeArtifact([], first), second)
  assert.deepEqual(list.map((x) => x.id), ['a', 'b'])
  list = mergeArtifact(list, { ...first, status: 'failed' })
  assert.equal(list.length, 2, '同 id 应替换而不是追加')
  assert.deepEqual(list.map((x) => x.id), ['a', 'b'], '位置不该变')
  assert.equal(list[0].status, 'failed', '内容要更新')
})

test('报告产物带正文,重开不需要再问模型', () => {
  const a = reportArtifact('turn-3', '600519 结构解读', '正文...')
  assert.equal(a.kind, 'report')
  assert.equal(a.payload.body, '正文...')
  assert.match(a.id, /^turn-3:/)
})

test('自动展开：ready 且空闲时展开', () => {
  const d = decideAutoOpen(READY, IDLE)
  assert.equal(d.open, true)
  assert.equal(d.announce, '600519', '要有可宣告的标题（aria-live 用）')
})

test('自动展开：失败的产物不开面板', () => {
  // 开一个空图比不开更糟；对话里会留失败卡片
  const d = decideAutoOpen({ ...READY, status: 'failed' }, IDLE)
  assert.equal(d.open, false)
  assert.equal(d.reason, 'failed')
})

test('自动展开：一轮只开第一个', () => {
  // annotate_chart 一轮可被调用多次（每只票一次）,不限制就会连跳三次
  const d = decideAutoOpen(READY, { ...IDLE, openedThisTurn: true })
  assert.equal(d.open, false)
  assert.equal(d.reason, 'already-opened')
})

test('自动展开：用户关过就不再弹', () => {
  const d = decideAutoOpen(READY, { ...IDLE, dismissedThisTurn: true })
  assert.equal(d.open, false)
  assert.equal(d.reason, 'dismissed')
})

test('自动展开：正在看别的产物时不切走', () => {
  const d = decideAutoOpen(READY, { ...IDLE, viewing: 't1:other' })
  assert.equal(d.open, false)
  assert.equal(d.reason, 'viewing-other')
  // 但正在看的就是它自己时,应该照常（比如重画后刷新）
  assert.equal(decideAutoOpen(READY, { ...IDLE, viewing: 't1:c1' }).open, true)
})

test('自动展开：窄窗口不分栏', () => {
  assert.equal(decideAutoOpen(READY, { ...IDLE, width: MIN_SPLIT_WIDTH - 1 }).reason, 'too-narrow')
  assert.equal(decideAutoOpen(READY, { ...IDLE, width: MIN_SPLIT_WIDTH }).open, true)
})

test('「用户关过」优先于「已经开过」', () => {
  // 两个条件同时成立时,reason 应反映用户的意图而不是碰巧先判到的那条
  const d = decideAutoOpen(READY, { ...IDLE, openedThisTurn: true, dismissedThisTurn: true })
  assert.equal(d.reason, 'dismissed')
})

test('新一轮重置本轮状态,但保留用户正在看什么', () => {
  const next = resetForTurn({ openedThisTurn: true, dismissedThisTurn: true, viewing: 'x', width: 1400 })
  assert.equal(next.openedThisTurn, false)
  assert.equal(next.dismissedThisTurn, false)
  assert.equal(next.viewing, 'x', 'viewing 是跨轮状态,不该被重置')
})

// ---- 接线（静态断言：这些是容易在重构里悄悄退化的连接点）----

const SRC = (rel) => readFileSync(R(...rel.split('/')), 'utf8')

test('tool_start 不再直接开 K 线图', () => {
  // 旧实现在 tool_start 就 openKline —— 工具还没成功,失败会留空面板;
  // 而且 action=list 也会弹开图表页。现在由 tool_result 之后的产物事件决定。
  const useChat = SRC('lib/useChat.ts')
  const block = useChat.match(/if \(type === 'tool_start'\)[\s\S]*?\n    \}/)
  assert.ok(block, '找不到 tool_start 分支')
  // 只看代码,不看注释 —— 注释里解释「旧实现在这里 openKline」是应该保留的
  const codeOnly = block[0].split('\n').filter((l) => !l.trim().startsWith('//')).join('\n')
  assert.ok(!/openKline/.test(codeOnly), 'tool_start 里不该再调 openKline')
  // 但缓存失效必须留着 —— 那条路径和产物无关
  assert.match(block[0], /invalidateOnTool/, '改持仓的工具仍要作废缓存')
})

test('报告也走注册表,不绕过自动展开策略', () => {
  const useChat = SRC('lib/useChat.ts')
  const branch = useChat.match(/if \(looksLikeReport\(body\)\)[\s\S]*?\n        \}/)
  assert.ok(branch, '找不到报告分支')
  assert.match(branch[0], /artifactsApi\.add\(reportArtifact\(/, '报告应进注册表')
  assert.ok(
    !/WyckoffApp\?\.openReport/.test(branch[0]),
    '直接调 openReport 会绕过「一轮只开第一个」「用户关过不再弹」等规则'
  )
})

test('shell 提供统一的 openArtifact 入口', () => {
  const shell = SRC('shell.js')
  assert.match(shell, /function openArtifact/, '缺少统一入口')
  assert.match(shell, /openArtifact: \(artifact\)/, '没挂到 WyckoffShell 上')
  // 两种 kind 都要能路由
  const fn = shell.match(/function openArtifact[\s\S]*?\n\}/)[0]
  assert.match(fn, /kind === 'kline'/)
  assert.match(fn, /kind === 'report'/)
})

test('自动展开时宣告给读屏,且不移动焦点', () => {
  const app = SRC('components/App.tsx')
  assert.match(app, /id="artifact-live"/, '缺少 aria-live 宣告区')
  assert.match(app, /aria-live="polite"/, '应为 polite —— 这是提示不是警报')
  const hook = SRC('lib/useArtifacts.ts')
  assert.match(hook, /getElementById\('artifact-live'\)/)
  // 宣告不能顺手把焦点抢过去
  assert.ok(!/\.focus\(\)/.test(hook), '自动展开不该移动焦点（用户可能正在打字）')
})

test('宣告区对读屏可见,但视觉上不占位', () => {
  // 用 display:none 会让读屏也读不到 —— 那样这块就白加了
  const css = SRC('app.css')
  const rule = css.match(/\.sr-only \{[\s\S]*?\}/)
  assert.ok(rule, '缺少 .sr-only 样式')
  assert.ok(!/display:\s*none/.test(rule[0]), 'display:none 会让读屏也读不到')
  assert.match(rule[0], /clip:/, '应该用裁剪把它移出视觉流')
})

test('K 线产物在对话里有卡片 —— 否则第二三只票没有入口', () => {
  const stream = SRC('components/ChatStream.tsx')
  assert.match(stream, /function KlineCard/, '缺少 K 线卡片')
  assert.match(stream, /a\.id\.startsWith\(`\$\{turn\.id\}:`\)/, '卡片要按轮次筛选')
  const card = stream.match(/function KlineCard[\s\S]*?\n\}/)[0]
  assert.match(card, /onOpen\?\.\(artifact\)/, '卡片要能打开对应产物')
  // 失败的产物不给「打开」按钮 —— 点了也没东西看
  assert.match(card, /failed \? null :/, '失败态不该有打开按钮')
})

test('淘汰页签时不能淘汰用户正在看的那个', () => {
  // 产物可以连着来（一轮画多只票）。正在读的图被悄悄换掉是最费解的失败 ——
  // 用户没做任何操作,内容就变了。
  const tabs = SRC('tabs.js')
  const evict = tabs.match(/if \(this\.tabs\.length > MAX_TABS\)[\s\S]*?\n    \}/)
  assert.ok(evict, '找不到淘汰逻辑')
  assert.match(evict[0], /tab\.key !== this\.activeId/, '淘汰候选要排除当前查看的页签')
})

test('面板宽度按产物类型分别记忆', () => {
  // K 线要横向空间看趋势,报告是竖排文本 —— 共用一个宽度时,看完图再看报告
  // 会觉得行太长（或反之图被压扁）。
  const shell = SRC('shell.js')
  assert.match(shell, /KIND_WIDTH_RATIO/, '缺少按类型的默认比例')
  assert.match(shell, /kline: 0\.52/)
  assert.match(shell, /report: 0\.46/)
  assert.match(shell, /function applyKindWidth/, '打开产物时要套用对应宽度')
  // 手动拖过的宽度是明确偏好,不该被下次自动展开覆盖
  assert.match(shell, /localStorage\.setItem\(kindWidthKey\(currentPaneKind\)/, '拖动后要记到类型名下')
})

test('产物类型清单只有一处,避免「加一半」', () => {
  // 加 dashboard 时我改了后端、shell、宽度表,却漏了 parseArtifactEvent 里
  // 那行 `kind !== 'kline' && kind !== 'report'` —— 事件被静默丢掉,面板压根
  // 不开,而且没有任何报错。重复清单必然导致这种漏改。
  const src = SRC('lib/artifacts.ts')
  assert.match(src, /export const ARTIFACT_KINDS/, '缺少统一清单')
  assert.match(src, /ARTIFACT_KINDS as readonly string\[\]\)\.includes\(kind\)/, '校验应从清单派生')
  // 不该再有写死的 kind 比较。只看代码 —— 注释里引用旧写法是有意保留的说明。
  const codeOnly = src.split('\n').filter((l) => {
    const t = l.trim()
    return !t.startsWith('//') && !t.startsWith('*') && !t.startsWith('/*')
  }).join('\n')
  assert.ok(
    !/kind !== '[a-z]+' && kind !== '[a-z]+'/.test(codeOnly),
    '还有写死的 kind 白名单 —— 加新类型时会漏改'
  )
})

test('dashboard 事件能被解析出来', () => {
  const a = parseArtifactEvent({
    type: 'chat_artifact', artifact_id: 't1:d1', kind: 'dashboard',
    title: '行业分布', status: 'ready', payload: { html: '<p>x</p>' }
  })
  assert.ok(a, 'dashboard 应被识别为合法产物')
  assert.equal(a.kind, 'dashboard')
  assert.equal(a.payload.html, '<p>x</p>')
})

test('shell 能路由 dashboard,且宽度与 K 线同档', () => {
  const shell = SRC('shell.js')
  const fn = shell.match(/function openArtifact[\s\S]*?\n\}/)[0]
  assert.match(fn, /kind === 'dashboard'/, 'openArtifact 要能路由 dashboard')
  // 面板通常是表格/图表,需要横向空间
  assert.match(shell, /dashboard: 0\.52/)
})

test('收起面板时两个原生 view 都要摘掉', () => {
  // 浏览器和可交互面板都浮在 DOM 之上。漏掉任何一个都表现为
  // 「面板关了但内容还飘在会话上」—— DOM 容器已不可见,view 却还在。
  const shell = SRC('shell.js')
  const setPane = shell.match(/function setPane \([\s\S]*?\n\}/)[0]
  assert.match(setPane, /browser\.hide\(\)/)
  assert.match(setPane, /artifact\.hide\(\)/, '收起面板时没有摘掉可交互产物视图')
})

test('dashBox 声明在模块顶层,不在使用点之后', () => {
  // 真实踩过的坑：顶层的 setPane(false) 会调 syncDashBounds(),而 let 有 TDZ。
  // 声明放在后面会让整个 shell.js 抛 "Cannot access 'dashBox' before
  // initialization" —— window.WyckoffShell 压根不被赋值,K 线/报告/面板全打不开,
  // 而且渲染端只有一行看起来无关的报错。
  const shell = SRC('shell.js')
  const declAt = shell.indexOf('let dashBox')
  assert.ok(declAt > 0, '找不到 dashBox 声明')
  // 关键是「顶层立即执行的那句」在声明之后 —— setPane(false) 是模块顶层调用,
  // 它会走到 syncDashBounds()。函数定义本身出现在哪不重要（提升到函数作用域）,
  // 重要的是 let 的 TDZ。
  const topLevelCall = shell.indexOf('\nsetPane(false)')
  assert.ok(topLevelCall > 0, '找不到顶层的 setPane(false)')
  assert.ok(
    declAt < topLevelCall,
    `dashBox 声明(${declAt}) 必须早于顶层 setPane(false)(${topLevelCall})，否则 TDZ 会打挂整个 shell.js`
  )
})
