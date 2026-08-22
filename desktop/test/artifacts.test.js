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
