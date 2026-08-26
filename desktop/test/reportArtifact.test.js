'use strict'

// 报告送去右侧面板时,正文不能从对话里消失。
//
// 原来那条路径是 `blocks.filter(b => b.kind !== 'text')` —— 把正文**整块滤掉**,
// 只留一行「已在右侧打开 →」,而那行是纯文本、不可点。两个后果:
//
// 1. openReport 一旦失败（渲染抛异常、面板被关掉）,模型生成的完整正文**彻底
//    没了**。那一轮只剩一句提示,重开无门,只能重新问一遍模型。
//    这是数据丢失,不是导航不便。
// 2. 用户主动关掉页签之后同样找不回来 —— K 线更彻底,drewCharts 只被写入、
//    没有任何组件读它,对话里连一行痕迹都不留。
//
// 现在正文存进 artifact 块,卡片上的按钮拿它重开,不需要再走模型。
const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')

const R = (...p) => join(__dirname, '..', 'src', 'renderer', ...p)
const USE_CHAT = readFileSync(R('lib', 'useChat.ts'), 'utf8')
const CHAT = readFileSync(R('lib', 'chat.ts'), 'utf8')
const STREAM = readFileSync(R('components', 'ChatStream.tsx'), 'utf8')

test('artifact 块带着正文,不是只带标题', () => {
  // 光有 title 的话「重新打开」就得再问一次模型 —— 那不叫重新打开。
  const decl = CHAT.match(/\| \{ kind: 'artifact';[^}]*\}/)
  assert.ok(decl, '找不到 artifact 块的类型声明')
  assert.match(decl[0], /body: string/, 'artifact 块必须带 body')
  assert.match(decl[0], /title: string/)
})

test('done 分支不再把正文搬去面板', () => {
  // 原来这里靠 looksLikeReport() 猜：超 400 字且含标题就当报告，正文
  // `.filter(b => b.kind !== 'text')` 从对话里删掉。用户问「我的持仓怎么了」，
  // 结果主聊天空白、答案在右侧面板里 —— 那段正文本来就是对提问的答复。
  const done = USE_CHAT.slice(USE_CHAT.indexOf("if (type === 'done')"))
  const code = done.replace(/\/\/[^\n]*/g, '')
  assert.ok(!/looksLikeReport/.test(code), 'done 分支不该再猜报告')
  assert.ok(!/kind !== 'text'/.test(code), '不得把正文块滤掉')
})

test('对话里的卡片能重新打开,且不重新调用模型', () => {
  assert.match(STREAM, /function ArtifactCard/, '缺少产物卡片组件')
  const card = STREAM.match(/function ArtifactCard[\s\S]*?\n\}/)
  assert.ok(card, '找不到 ArtifactCard 实现')
  // 重开必须直接用手里的 body,不能触发一次新的对话
  assert.match(card[0], /openReport\?\.\(title, body\)/, '重开应直接用已有正文')
  assert.ok(!/send\(|collect\(/.test(card[0]), '重开不该触发新的模型调用')
  // 卡片要走 case 分支才真的会渲染
  assert.match(STREAM, /case 'artifact':/, 'BlockView 没有处理 artifact 块')
})

test('重开按钮的文案中英各一条', () => {
  const locales = readFileSync(R('locales.js'), 'utf8')
  const hits = locales.split("'chat.reopen'").length - 1
  assert.equal(hits, 2, `chat.reopen 应中英各一条,实际 ${hits} 条`)
})
