'use strict'

// 产物 id 必须和对话轮次 id 落在同一命名空间。
//
// 这份测试刻意**跨两端**：用后端真实产出的 id 形状，配上前端真实的 turn.id
// 形状，验证卡片筛选条件能不能匹配上。
//
// 为什么必须这样测：原来的测试都是自己造 id 自己验（'t1:c1'），两端各自
// 「自洽」但拼不上 —— 后端发 `turn-1:call_7`，前端 turn.id 是 IPC stream id
// （数字，如 17），`startsWith('17:')` 恒为假。结果 K 线卡片一个都不出现、
// toolMadeReport 判断永远为假，而 20 条静态断言全绿。
//
// 静态断言只能锁住「我以为的契约」，锁不住「两端实际用的是不是同一个东西」。
const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const ts = require('typescript')

const R = (...p) => join(__dirname, '..', 'src', 'renderer', ...p)

function load (rel) {
  const js = ts.transpileModule(readFileSync(R('lib', rel), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
  }).outputText
  const mod = { exports: {} }
  new Function('module', 'exports', 'require', js)(mod, mod.exports, require)
  return mod.exports
}

const { parseArtifactEvent, artifactId } = load('artifacts.ts')

// python-bridge 的请求 id 是自增计数器 —— 数字，经 String() 变成 '17'。
const STREAM_ID = '17'

test('产物 id 由前端用它已知的 stream id 拼出', () => {
  // 后端只给 call_id（它不知道传输层的请求 id —— 那在 stdio 层）。
  // 前端知道事件属于哪一轮（分发就是靠它），所以由前端拼。
  assert.equal(typeof artifactId, 'function', '缺少 artifactId 组合函数')
  assert.equal(artifactId(STREAM_ID, 'call_7'), '17:call_7')
})

test('解析出来的产物 id 与 turn.id 前缀匹配', () => {
  // 这条就是那个断层：换成后端自己编的 turn-1 前缀就会红。
  const artifact = parseArtifactEvent({
    type: 'chat_artifact',
    id: STREAM_ID,            // 传输层塞的流 id
    artifact_call_id: 'call_7',
    kind: 'kline',
    title: '600519',
    status: 'ready',
    payload: { symbol: '600519' }
  })
  assert.ok(artifact, '事件应能解析')
  assert.ok(
    artifact.id.startsWith(`${STREAM_ID}:`),
    `产物 id (${artifact.id}) 必须以 turn.id (${STREAM_ID}) 开头，否则卡片永远不显示`
  )
})

test('同一轮的多次调用给出不同 id，跨轮不撞', () => {
  const a = artifactId('17', 'call_1')
  const b = artifactId('17', 'call_2')
  const c = artifactId('18', 'call_1')
  assert.notEqual(a, b, '同轮不同调用应区分')
  assert.notEqual(a, c, '不同轮次不能撞')
})

test('缺少 call_id 时退回一个稳定值，而不是产出无前缀的 id', () => {
  // 宁可同轮多次调用互相覆盖，也不能产出前缀对不上的 id ——
  // 后者会让卡片静默消失，比覆盖更难查。
  const a = artifactId('17', '')
  assert.ok(a.startsWith('17:'), `退化情况也必须带前缀，实际 ${a}`)
})
