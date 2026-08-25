'use strict'

// 会话列表的纯逻辑。排序里最容易错的是置顶与时间的优先级，以及 SQLite 时间戳
// 没有时区标记这件事 —— 直接 new Date() 会把刚发生的事显示成「8小时前」。
const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const ts = require('typescript')

const SRC = join(__dirname, '..', 'src', 'renderer', 'lib', 'sessions.ts')
const js = ts.transpileModule(readFileSync(SRC, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 }
}).outputText
const mod = { exports: {} }
new Function('module', 'exports', 'require', js)(mod, mod.exports, require)
const { displayTitle, sortSessions, filterSessions, relativeTime, nextAfterRemoval } = mod.exports

const S = (over = {}) => ({
  session_id: 's1', title: '标题', pinned: 0, msg_count: 2,
  started_at: '2026-08-20 10:00:00', ended_at: '2026-08-20 10:05:00', ...over
})

test('标题为空时回落到首条提问', () => {
  assert.equal(displayTitle(S({ title: '', first_user_msg: '看看茅台' })), '看看茅台')
})

test('首条提问只取第一行', () => {
  // 真实数据里提问后面带注入的时间戳上下文。后端已清洗，这里是老数据兜底。
  const s = S({ title: '', first_user_msg: '怎么报销？\n\n[当前北京时间：2026-08-21]' })
  assert.equal(displayTitle(s), '怎么报销？')
})

test('两者都空时给明确占位，而不是空白条目', () => {
  assert.equal(displayTitle(S({ title: '', first_user_msg: '' })), '未命名对话')
})

test('置顶排在最前，不管时间', () => {
  const rows = [
    S({ session_id: 'new', ended_at: '2026-08-24 12:00:00' }),
    S({ session_id: 'old-pinned', ended_at: '2026-08-01 09:00:00', pinned: 1 })
  ]
  assert.deepEqual(sortSessions(rows).map((s) => s.session_id), ['old-pinned', 'new'])
})

test('非置顶按最后活动时间倒序', () => {
  const rows = [
    S({ session_id: 'a', ended_at: '2026-08-20 10:00:00' }),
    S({ session_id: 'b', ended_at: '2026-08-24 10:00:00' }),
    S({ session_id: 'c', ended_at: '2026-08-22 10:00:00' })
  ]
  assert.deepEqual(sortSessions(rows).map((s) => s.session_id), ['b', 'c', 'a'])
})

test('按最后活动而不是创建时间排序', () => {
  // 上周开的会话今天又聊了，用户找的是「刚才聊的那个」。
  const rows = [
    S({ session_id: 'today-new', started_at: '2026-08-24 09:00:00', ended_at: '2026-08-24 09:10:00' }),
    S({ session_id: 'old-but-active', started_at: '2026-08-01 09:00:00', ended_at: '2026-08-24 15:00:00' })
  ]
  assert.equal(sortSessions(rows)[0].session_id, 'old-but-active')
})

test('排序不改原数组', () => {
  const rows = [S({ session_id: 'a' }), S({ session_id: 'b', pinned: 1 })]
  sortSessions(rows)
  assert.equal(rows[0].session_id, 'a')
})

test('搜索匹配标题和首条提问两处', () => {
  const rows = [
    S({ session_id: 'by-title', title: '季报解读', first_user_msg: '看看这家' }),
    S({ session_id: 'by-msg', title: '风控设置', first_user_msg: '帮我设个止损' })
  ]
  assert.deepEqual(filterSessions(rows, '季报').map((s) => s.session_id), ['by-title'])
  assert.deepEqual(filterSessions(rows, '止损').map((s) => s.session_id), ['by-msg'])
})

test('搜索大小写不敏感', () => {
  const rows = [S({ title: 'AAPL 结构' })]
  assert.equal(filterSessions(rows, 'aapl').length, 1)
})

test('空查询返回全部', () => {
  const rows = [S({ session_id: 'a' }), S({ session_id: 'b' })]
  assert.equal(filterSessions(rows, '   ').length, 2)
})

test('SQLite 的无时区时间戳按 UTC 解读', () => {
  // 这是最容易错的一处：SQLite datetime('now') 是 UTC 但不带 Z。
  // 当成本地时间会让刚发生的事显示成几小时前。
  const now = Date.UTC(2026, 7, 24, 12, 0, 0)
  assert.equal(relativeTime('2026-08-24 11:30:00', now), '30分钟前')
})

test('相对时间的各档', () => {
  const now = Date.UTC(2026, 7, 24, 12, 0, 0)
  assert.equal(relativeTime('2026-08-24 11:59:40', now), '刚刚')
  assert.equal(relativeTime('2026-08-24 09:00:00', now), '3小时前')
  assert.equal(relativeTime('2026-08-21 12:00:00', now), '3天前')
  assert.equal(relativeTime('2026-06-24 12:00:00', now), '2个月前')
})

test('坏时间戳不抛异常', () => {
  assert.equal(relativeTime(''), '')
  assert.equal(relativeTime('not-a-date'), '')
})

test('归档/删除的不是当前会话时不切走', () => {
  // 用户在整理列表，不该被拽到别的对话去。
  const rows = [S({ session_id: 'a' }), S({ session_id: 'b' })]
  assert.equal(nextAfterRemoval(rows, 'b', 'a'), 'a')
})

test('离开的是当前会话时切到列表里的下一个', () => {
  const rows = [
    S({ session_id: 'a', ended_at: '2026-08-24 10:00:00' }),
    S({ session_id: 'b', ended_at: '2026-08-23 10:00:00' })
  ]
  assert.equal(nextAfterRemoval(rows, 'a', 'a'), 'b')
})

test('列表里最后一个会话离开时返回空串', () => {
  // 调用方据此开新会话 —— 不能让下一轮对话写进一个已经不在列表里的 id。
  assert.equal(nextAfterRemoval([S({ session_id: 'only' })], 'only', 'only'), '')
})
