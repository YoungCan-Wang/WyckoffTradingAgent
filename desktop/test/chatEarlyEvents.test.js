'use strict'

// send() 要 await 桥返回的 {ok, id} 才能建起这一轮，但桥一收到请求就**同步**
// 开始推事件 —— 所以头几个 text_delta 一定早于那一行。它们既进不了 liveIds
// 判断（那时还没登记），也找不到对应的 turn，于是被静默丢掉。
//
// 症状极具欺骗性：界面看起来正常，只是正文从中间某句开始，前面的段落和列表项
// 凭空消失。我是因为渲染出来的 <li> 只有 2 个（应该 3 个）才发现的 —— 肉眼
// 看那段话是通顺的。
//
// 这组测试锁住「飞行期间的事件要缓存并回放」。
const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')

const SRC = readFileSync(
  join(__dirname, '..', 'src', 'renderer', 'lib', 'useChat.ts'), 'utf8'
)

test('有 send 在飞行时，未知 id 的事件要缓存而不是丢弃', () => {
  assert.match(SRC, /sendInFlight/, '需要一个「send 正在飞行」的标记')
  // 关键：守卫里遇到不认识的 id，飞行期间要落进缓存
  assert.match(
    SRC,
    /if \(!liveIds\.current\.has\(id\)\) \{[\s\S]{0,400}pendingEvents\.current\.set/,
    'liveIds 判断失败时应缓存（飞行期间），而不是直接 return'
  )
  assert.match(SRC, /if \(!sendInFlight\.current\) return/, '没有飞行中的 send 才该丢弃')
})

test('id 回来后要回放缓存的事件', () => {
  assert.match(SRC, /const early = pendingEvents\.current\.get\(id\)/, '应取出该 id 缓存的事件')
  assert.match(SRC, /early\.reduce\(\(acc, e\) => applyEvent\(acc, e\), x\)/, '应按顺序逐条回放')
  // 顺序很重要：先建 turn 再回放，否则 map 又找不到它
  const addIdx = SRC.indexOf('liveIds.current.add(id)')
  const replayIdx = SRC.indexOf('const early = pendingEvents.current.get(id)')
  assert.ok(addIdx > 0 && replayIdx > addIdx, '必须先登记并建好 turn，再回放')
})

test('飞行计数在失败路径上也要归零', () => {
  // 早期版本把 -= 1 写在成功分支里，一次失败就让缓存永久增长
  assert.match(
    SRC,
    /\.finally\(\(\) => \{\s*sendInFlight\.current -= 1/,
    '应用 finally 归零，否则调用失败后计数漏掉、缓存无界增长'
  )
})

test('回放后清空缓存', () => {
  // 没被认领的事件属于页面调用（它们有自己的订阅），留着只占内存
  assert.match(SRC, /pendingEvents\.current\.clear\(\)/, '回放后应清空缓存')
})
