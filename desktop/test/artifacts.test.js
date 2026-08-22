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
