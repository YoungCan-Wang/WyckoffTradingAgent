'use strict'

// 对话流的状态模型。vanilla 版是 `textContent += delta` 增量改 DOM，
// 转 React 后必须靠状态累积 —— 合并逻辑写错会产出几百个空段落，或者把
// 工具行插到错误的位置。
const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const ts = require('typescript')

const SRC = join(__dirname, '..', 'src', 'renderer', 'lib', 'chat.ts')
const js = ts.transpileModule(readFileSync(SRC, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 }
}).outputText
const mod = { exports: {} }
new Function('module', 'exports', 'require', js)(mod, mod.exports, require)
const { applyEvent, looksLikeReport, reportTitle, finalText, isPortfolioWriteTool } = mod.exports

const turn = () => ({ id: 't1', blocks: [], live: true })

test('文字增量合并成一个块', () => {
  let s = turn()
  for (const c of ['你', '好', '世界']) s = applyEvent(s, { type: 'text_delta', text: c })
  assert.equal(s.blocks.length, 1, '每个 delta 建一个块会产出几百个段落')
  assert.equal(s.blocks[0].text, '你好世界')
})

test('思考与正文是两个块，不互相污染', () => {
  let s = turn()
  s = applyEvent(s, { type: 'thinking_delta', text: '想一下' })
  s = applyEvent(s, { type: 'text_delta', text: '结论' })
  assert.deepEqual(s.blocks.map((b) => b.kind), ['thinking', 'text'])
})

test('工具行打断文字后，后续文字是新块', () => {
  let s = turn()
  s = applyEvent(s, { type: 'text_delta', text: '先看数据' })
  s = applyEvent(s, { type: 'tool_start', name: 'portfolio', display_name: '读持仓' })
  s = applyEvent(s, { type: 'text_delta', text: '结论是' })
  // 顺序必须是到达顺序 —— 工具在中间出现，界面上也该在中间
  assert.deepEqual(s.blocks.map((b) => b.kind), ['text', 'tool', 'text'])
  assert.equal(s.blocks[0].text, '先看数据')
  assert.equal(s.blocks[2].text, '结论是')
})

test('不改原对象', () => {
  const a = turn()
  const b = applyEvent(a, { type: 'text_delta', text: 'x' })
  assert.equal(a.blocks.length, 0, '就地修改会让 React 收不到变更')
  assert.equal(b.blocks.length, 1)
})

test('不认识的事件原样返回，不造块', () => {
  const a = turn()
  const b = applyEvent(a, { type: 'brand_new_event', foo: 1 })
  assert.equal(b.blocks.length, 0)
  assert.equal(b, a, '没变化就该返回同一个对象')
})

test('工具失败与普通错误分开', () => {
  let s = turn()
  s = applyEvent(s, { type: 'tool_error', name: 'kline', error: '超时' })
  s = applyEvent(s, { type: 'error', message: '模型断了' })
  assert.deepEqual(s.blocks.map((b) => b.kind), ['toolError', 'error'])
  assert.equal(s.blocks[0].error, '超时')
})

test('审批事件整条留着 —— 卡片要用里面的参数', () => {
  const s = applyEvent(turn(), {
    type: 'approval_pending', id: 7, approval_id: 'ap1', summary: '卖出', args: { code: '600519' }
  })
  assert.equal(s.blocks[0].kind, 'approval')
  assert.equal(s.blocks[0].event.approval_id, 'ap1')
  assert.equal(s.blocks[0].event.args.code, '600519')
})

test('报告判定：短文本不算', () => {
  assert.equal(looksLikeReport('# 标题\n很短'), false)
  assert.equal(looksLikeReport(''), false)
  assert.equal(looksLikeReport(undefined), false)
})

test('报告判定：够长且有标题或表格', () => {
  const long = 'x'.repeat(400)
  assert.equal(looksLikeReport('# 标题\n' + long), true)
  assert.equal(looksLikeReport('| a | b |\n' + long), true)
  // 够长但没结构 —— 那就是普通长回复，留在对话里
  assert.equal(looksLikeReport(long), false)
})

test('报告标题：取一级标题并截断', () => {
  assert.equal(reportTitle('# 今日复盘\n正文', '兜底'), '今日复盘')
  assert.equal(reportTitle('## 二级也行\n正文', '兜底'), '二级也行')
  assert.equal(reportTitle('没有标题', '兜底'), '兜底')
  assert.ok(reportTitle('# ' + 'x'.repeat(50), '兜底').endsWith('…'))
})

test('最终正文：done 带的优先', () => {
  let s = turn()
  s = applyEvent(s, { type: 'text_delta', text: '流式的' })
  assert.equal(finalText(s, '完整的'), '完整的')
  assert.equal(finalText(s), '流式的')
})

test('最终正文：只拼文字块，不含思考', () => {
  let s = turn()
  s = applyEvent(s, { type: 'thinking_delta', text: '内心戏' })
  s = applyEvent(s, { type: 'text_delta', text: '正文' })
  assert.equal(finalText(s), '正文')
})

test('改持仓的工具名单', () => {
  for (const n of ['update_portfolio', 'set_stop_loss', 'record_trade_fill']) {
    assert.equal(isPortfolioWriteTool(n), true, n)
  }
  for (const n of ['portfolio', 'kline', '', undefined]) {
    assert.equal(isPortfolioWriteTool(n), false, String(n))
  }
})
