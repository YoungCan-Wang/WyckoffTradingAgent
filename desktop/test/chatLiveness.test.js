/**
 * 对话里的「进展感」。
 *
 * 用户的原话:「目前这些 UI 都是静态的…要不然就像页面卡死了,没有活人感。」
 * 根因有两条,都是后端一直在发、前端收不到:
 *   1. tool_result 不在 IPC 白名单 → 工具「在跑」和「跑完」长得一模一样
 *   2. stage_start/stage_done 不在白名单 → 模型生成工具调用那 20 秒只有静止的
 *      「正在思考…」
 */
'use strict'
const test = require('node:test')
const assert = require('node:assert')
const fs = require('node:fs')
const path = require('node:path')

const ROOT = path.join(__dirname, '..', '..')
const SRC = (rel) => fs.readFileSync(path.join(__dirname, '..', 'src', 'renderer', rel), 'utf8')
const PY = (rel) => fs.readFileSync(path.join(ROOT, rel), 'utf8')

test('tool_result 与 stage 事件必须过 IPC 白名单', () => {
  const s = PY('cli/ipc/session.py')
  const block = s.slice(s.indexOf('_PASSTHROUGH = {'), s.indexOf('_PASSTHROUGH = {') + 900)
  assert.match(block, /"tool_result":/, '不放行就永远不知道工具跑完了')
  assert.match(block, /"stage_start":/, '不放行就只能显示泛泛的「正在思考」')
})

test('tool_result 不带结果正文过河', () => {
  // 持仓明细、K 线数据动辄几十 KB；界面只需要「做完了」这一个比特。
  // 要展示的东西走产物事件，不走这里。
  const s = PY('cli/ipc/session.py')
  const line = s.split('\n').find((l) => l.includes('"tool_result":'))
  // 只看冒号右边的字段元组 —— 键名本身就含 "result"，整行匹配会误判
  const fields = line.slice(line.indexOf(':') + 1)
  assert.ok(!/"result"/.test(fields), `tool_result 不该放 result 字段: ${line}`)
})

test('工具块有完成状态，且在跑与跑完视觉不同', () => {
  const c = SRC('lib/chat.ts')
  assert.match(c, /kind: 'tool'[^}]*done\?: boolean/, 'Block 要能表达完成')
  assert.match(c, /case 'tool_result':/)
  const v = SRC('components/ChatStream.tsx')
  assert.match(v, /block\.done \? 'tool done' : 'tool running'/, '两种状态要有不同的类名')
  const css = SRC('app.css')
  assert.match(css, /\.tool\.running \.g \{[^}]*animation/, '在跑要有动效')
  assert.match(css, /\.tool\.done \.g \{[^}]*animation: none/, '跑完要停下来')
})

test('同名工具连续调用不会把结果记错行', () => {
  // 同一轮可能两只票各查一次持仓；从前往后找会让第一行提前变对勾、
  // 第二行永远转圈。
  const c = SRC('lib/chat.ts')
  const fn = c.slice(c.indexOf('export function markToolDone'), c.indexOf('export function pushBlock'))
  assert.match(fn, /for \(let i = blocks\.length - 1; i >= 0; i--\)/, '必须从后往前找')
  assert.match(fn, /!b\.done/, '要跳过已完成的块')
})

test('等待提示在工具跑完后仍然显示', () => {
  // 工具跑完、模型继续想下一步时 blocks 已有内容，原来占位就消失了，
  // 界面又静下来 —— 那正是「看着像卡死」的第二段。
  const v = SRC('components/ChatStream.tsx')
  assert.match(v, /turn\.live && \(!turn\.blocks\.length \|\| turn\.stage\)/,
    '有阶段进行中时也要显示等待行')
})

test('等待文案优先用后端阶段，且多轮带轮次', () => {
  const v = SRC('components/ChatStream.tsx')
  const fn = v.slice(v.indexOf('function waitLabel'), v.indexOf('function waitLabel') + 420)
  assert.match(fn, /turn\.stage/, '优先用后端的具体阶段文案')
  assert.match(fn, /round > 1/, '第 2 轮起显示轮次 —— 长任务里那是「还在推进」的唯一证据')
})

test('等待行本身有动效', () => {
  const css = SRC('app.css')
  const block = css.slice(css.indexOf('.chat-wait {'), css.indexOf('.chat-wait {') + 200)
  assert.match(block, /animation/, '静止的一行字和卡死的界面没有区别')
})
