'use strict'

// 计划任务的状态判定与 cron 描述。从 app.js 搬到 lib/schedules.ts 时行为必须
// 不变 —— 「未启用」被报成「失败」会让人以为系统坏了。
const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const ts = require('typescript')

// 组件里 t() 走 window.WyckoffI18n；这里只需要它把 key 原样回显，
// 断言判定逻辑而不是文案。
global.window = {
  WyckoffI18n: {
    t: (key, params) => (params && Object.keys(params).length
      ? `${key}:${JSON.stringify(params)}`
      : key),
    getLang: () => 'zh-CN'
  }
}

const SRC = join(__dirname, '..', 'src', 'renderer', 'lib', 'schedules.ts')
const js = ts.transpileModule(readFileSync(SRC, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 }
}).outputText
const mod = { exports: {} }
new Function('module', 'exports', 'require', js)(mod, mod.exports, require)
const { scheduleState, hasIssue, describeCron, riskReasonText, displayTime,
        buildCron, parseCron, DEFAULT_CADENCE } = mod.exports

test('状态：未启用不算失败', () => {
  // 这两个混在一起会让「我自己关掉的任务」显示成故障
  assert.equal(scheduleState({ enabled: false }).label, 'tasks.statusDisabled')
  assert.equal(scheduleState({ enabled: false }).tone, '')
})

test('状态：last_error 即失败，即使 status 是成功', () => {
  const s = scheduleState({ enabled: true, last_status: 'success', last_error: '数据源超时' })
  assert.equal(s.tone, 'failed')
  assert.equal(s.label, 'tasks.statusFailed')
})

test('状态：中英文失败关键词都认', () => {
  for (const v of ['failed', 'ERROR', '执行失败', '数据异常']) {
    assert.equal(scheduleState({ enabled: true, last_status: v }).tone, 'failed', v)
  }
})

test('状态：成功关键词', () => {
  for (const v of ['success', 'DONE', 'ok', '已完成', '成功']) {
    assert.equal(scheduleState({ enabled: true, last_status: v }).tone, 'success', v)
  }
})

test('状态：启用但没跑过 = 已启用，不是失败也不是成功', () => {
  const s = scheduleState({ enabled: true })
  assert.equal(s.tone, '')
  assert.equal(s.label, 'tasks.statusEnabled')
})

test('异常判定：只算已启用的', () => {
  // 关掉的任务即使上次失败也不该进「需要你处理」
  assert.equal(hasIssue({ enabled: false, last_status: 'failed' }), false)
  assert.equal(hasIssue({ enabled: true, last_status: 'failed' }), true)
  assert.equal(hasIssue({ enabled: true, last_error: 'boom' }), true)
  assert.equal(hasIssue({ enabled: true, last_status: 'success' }), false)
})

test('cron：工作日与每天分开', () => {
  assert.equal(describeCron('30 8 * * 1-5'), 'schedules.everyWeekday:{"time":"08:30"}')
  assert.equal(describeCron('5 15 * * *'), 'schedules.everyDay:{"time":"15:05"}')
})

test('cron：每 N 分钟', () => {
  assert.equal(describeCron('*/30 * * * *'), 'schedules.everyMinutes:{"count":"30"}')
})

test('cron：认不出就原样回显，不猜', () => {
  // 猜错比不翻译更糟 —— 用户会照着错的时间安排自己的事
  for (const bad of ['', 'garbage', '1 2 3', '0 0 1 1 1 1']) {
    assert.ok(describeCron(bad).startsWith('schedules.rawCron'), bad)
  }
  // 复杂但合法的 5 段式也走原样，因为我们没有对应的人话模板
  assert.ok(describeCron('0 9,15 * * 2').startsWith('schedules.rawCron'))
})

test('cron：时间补零', () => {
  assert.equal(describeCron('5 9 * * *'), 'schedules.everyDay:{"time":"09:05"}')
})

test('风险理由：白名单之外一律不显示', () => {
  // 不能把内部代号漏给用户
  assert.equal(riskReasonText({ id: 'x', risk_reason: 'reason.made_up' }), '')
  assert.equal(riskReasonText({ id: 'x', risk_reason: 'destructive_action' }), '', '缺 reason. 前缀应拒绝')
  assert.equal(riskReasonText({ id: 'x' }), '')
})

test('风险理由：占比只在与阈值相关的理由里带', () => {
  const over = riskReasonText({ id: 'x', risk_reason: 'reason.over_nav', nav_ratio: 0.083 })
  assert.ok(over.includes('8.3'), `应带百分比，实际 ${over}`)
  // 「清仓」不该硬贴一个百分比
  const destr = riskReasonText({ id: 'x', risk_reason: 'reason.destructive_action', nav_ratio: 0.5 })
  assert.equal(destr, 'approvals.reason.destructive_action')
})

test('风险理由：占比为 0 时不带百分比', () => {
  assert.equal(
    riskReasonText({ id: 'x', risk_reason: 'reason.over_nav', nav_ratio: 0 }),
    'approvals.reason.over_nav'
  )
})

test('时间显示：认不出的原样回显而不是 Invalid Date', () => {
  assert.equal(displayTime(''), '—')
  assert.equal(displayTime(undefined), '—')
  assert.equal(displayTime('不是时间'), '不是时间')
  assert.ok(displayTime('2026-08-20T09:31:00+08:00').length > 4)
})

// 下拉框取值 ↔ cron 互转。
//
// 界面上不出现 cron 表达式 —— 用户没有那个能力，也不该有。cron 只是序列化形式，
// 所以这层转换必须是可靠的往返：编辑一个已有任务时先 parse 再 build，
// 出来的必须和进去的一样，否则「点开编辑再保存」会悄悄改掉触发时间。
test('cron：三种重复方式都能编码', () => {
  assert.equal(buildCron({ repeat: 'weekday', hour: 9, minute: 25 }), '25 9 * * 1-5')
  assert.equal(buildCron({ repeat: 'weekly', weekday: 5, hour: 16, minute: 0 }), '0 16 * * 5')
  assert.equal(buildCron({ repeat: 'monthly', monthday: 1, hour: 9, minute: 0 }), '0 9 1 * *')
})

test('cron：编码时把越界值收进合法区间', () => {
  // 下拉框不该产出这些，但 buildCron 是公开函数，坏值落盘会打死整个调度轮次
  assert.equal(buildCron({ repeat: 'weekday', hour: 99, minute: -5 }), '0 23 * * 1-5')
  assert.equal(buildCron({ repeat: 'monthly', monthday: 99, hour: 9, minute: 0 }), '0 9 31 * *')
  assert.equal(buildCron({ repeat: 'weekly', weekday: 9, hour: 9, minute: 0 }), '0 9 * * 6')
})

test('cron：往返不变形', () => {
  for (const cron of ['25 9 * * 1-5', '0 16 * * 5', '5 15 * * 1-5', '0 9 1 * *', '30 23 28 * *', '0 0 * * 0']) {
    const parsed = parseCron(cron)
    assert.ok(parsed, `应该认得 ${cron}`)
    assert.equal(buildCron(parsed), cron, `${cron} 往返后变了`)
  }
})

test('cron：认不出的返回 null 而不是默认值', () => {
  // 静默改成默认值最糟：用户点开编辑、直接保存，触发时间就被换掉了
  for (const bad of ['*/15 * * * *', '0 9 * 3 *', '0 9 1 * 5', 'bogus', '', '1 2 3 4', '99 9 * * *']) {
    assert.equal(parseCron(bad), null, `${bad} 不该被认出`)
  }
})

test('cron 描述：每月几号不能说成每天', () => {
  // monthday 是数字时 weekday 也是 '*'，判定顺序反了就会串
  assert.match(describeCron('0 9 5 * *'), /everyMonthday/)
  assert.match(describeCron('0 9 * * *'), /everyDay/)
  assert.match(describeCron('25 9 * * 1-5'), /everyWeekday/)
  assert.match(describeCron('0 16 * * 5'), /everyWeekOn/)
})

test('默认值落在真实用途上', () => {
  // 09:25 是 A 股开盘前，和两个预置任务一致
  assert.equal(buildCron(DEFAULT_CADENCE), '25 9 * * 1-5')
})
