// 定时任务页：只有起真窗口才验得了的几件事。
//
// cron 编解码本身已经被单测盖住了（test/schedules.test.js）。这里验的是别的东西：
// 1. 下拉框选出来的东西，真的存进 schedules.json 变成对的 cron —— 三种重复方式各走一遍
// 2. 存完之后卡片上显示的周期，和刚才选的一致（编码解码没在真链路上打架）
// 3. 编辑能把已存的 cron 还原回下拉框（往返不丢）
// 4. 任务区和推荐区的列真的对齐 —— 这条只有量真实布局才算数
import { _electron as electron } from 'playwright'
import assert from 'node:assert/strict'
import { mkdtemp, readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { tmpdir } from 'node:os'

const APP_DIR = join(dirname(fileURLToPath(import.meta.url)), '..', '..')

let failures = 0
const write = (s = '') => process.stdout.write(`${s}\n`)
const check = (name, fn) => {
  try { fn(); write(`  ok   ${name}`) } catch (err) { failures += 1; write(`  FAIL ${name}: ${err.message}`) }
}

const profile = await mkdtemp(join(tmpdir(), 'wyckoff-sched-'))
// 干净的家目录 = 干净的 schedules.json，不会碰到开发机上真实的定时任务。
const home = await mkdtemp(join(tmpdir(), 'wyckoff-sched-home-'))
const app = await electron.launch({
  args: [APP_DIR, `--user-data-dir=${profile}`],
  env: { ...process.env, ELECTRON_DISABLE_GPU: '1', WYCKOFF_E2E_FAKE_SIGNIN: '1', HOME: home, USERPROFILE: home }
})
const win = await app.firstWindow()

write('定时任务页：')

await win.waitForSelector('.thread', { timeout: 30_000 })
await win.locator('.nv', { hasText: '定时任务' }).click()
await win.waitForSelector('[data-testid=schedule-new]', { timeout: 15_000 })

const stored = async (name) => {
  const raw = JSON.parse(await readFile(join(home, '.wyckoff', 'schedules.json'), 'utf8'))
  const list = Array.isArray(raw) ? raw : (raw.schedules ?? [])
  return list.find((s) => s.name === name) ?? null
}

// 新建一个任务：按给定的重复方式填完并保存。回传保存前的预览文案。
const createTask = async ({ name, repeat, pick, hour, minute }) => {
  await win.click('[data-testid=schedule-new]')
  await win.waitForSelector('form.sf', { timeout: 5_000 })
  await win.fill('form.sf input.sf-input', name)
  await win.fill('form.sf textarea.sf-textarea', `${name}：把该看的看一遍`)

  const repeatBox = win.locator('form.sf .sf-row', { hasText: '重复' }).locator('select').first()
  await repeatBox.selectOption(repeat)
  // 第二个下拉框（周几 / 几号）是随重复方式条件渲染的，选完第一个才存在。
  if (pick != null) {
    await win.locator('form.sf .sf-row', { hasText: '重复' }).locator('select').nth(1).selectOption(pick)
  }

  const timeRow = win.locator('form.sf .sf-row', { hasText: '时间' })
  await timeRow.locator('select').nth(0).selectOption(hour)
  await timeRow.locator('select').nth(1).selectOption(minute)

  const preview = (await win.textContent('form.sf .sf-preview'))?.trim()
  await win.locator('form.sf .mbtn.primary').click()
  await win.waitForSelector('form.sf', { state: 'detached', timeout: 10_000 })
  // 表单收起 ≠ 列表已刷新：保存后要再去后端拉一次列表，这中间卡片还不存在。
  // 不等的话读到的是空字符串，看着像「显示不出周期」，其实只是量早了。
  await win.locator('.schedule-card', { hasText: name }).waitFor({ timeout: 10_000 })
  return { preview, saved: await stored(name) }
}

// 三种重复方式各存一遍。cron 五段：分 时 日 月 周。
const weekly = await createTask({ name: 'E2E每周', repeat: 'weekly', pick: '5', hour: '16', minute: '25' })
check('每周：存下来的 cron 是周五 16:25', () => assert.equal(weekly.saved?.cron, '25 16 * * 5'))
check('每周：保存前的预览说得对', () => assert.match(weekly.preview ?? '', /每周五\s*16:25/))

const monthly = await createTask({ name: 'E2E每月', repeat: 'monthly', pick: '28', hour: '9', minute: '5' })
check('每月：存下来的 cron 落在 28 号', () => assert.equal(monthly.saved?.cron, '5 9 28 * *'))
check('每月：预览说的是几号，不是每天', () => assert.match(monthly.preview ?? '', /每月\s*28\s*号\s*09:05/))

const weekday = await createTask({ name: 'E2E工作日', repeat: 'weekday', pick: null, hour: '8', minute: '0' })
check('工作日：存下来的 cron 是 1-5', () => assert.equal(weekday.saved?.cron, '0 8 * * 1-5'))
check('工作日：预览说得对', () => assert.match(weekday.preview ?? '', /工作日\s*08:00/))

// 新建默认启用 —— 填完点保存那个意图就是「让它开始跑」。
check('新建的任务默认启用', () => assert.equal(weekly.saved?.enabled, true))

// 卡片上显示的周期，要和刚才选的一致。
const shown = await win.evaluate(() => {
  const out = {}
  for (const card of document.querySelectorAll('.schedule-card')) {
    const name = card.querySelector('.schedule-name')?.textContent?.trim()
    if (name?.startsWith('E2E')) out[name] = card.querySelector('.schedule-cadence')?.textContent?.trim()
  }
  return out
})
check('每周：卡片显示与所选一致', () => assert.match(shown['E2E每周'] ?? '', /每周五\s*16:25/))
check('每月：卡片显示与所选一致', () => assert.match(shown['E2E每月'] ?? '', /每月\s*28\s*号\s*09:05/))
check('工作日：卡片显示与所选一致', () => assert.match(shown['E2E工作日'] ?? '', /工作日\s*08:00/))

// 编辑要能把存下来的 cron 还原回下拉框 —— 往返不丢。
await win.locator('.schedule-card', { hasText: 'E2E每月' }).locator('.mbtn', { hasText: '编辑' }).click()
await win.waitForSelector('form.sf', { timeout: 5_000 })
const restored = await win.evaluate(() => {
  const sels = [...document.querySelectorAll('form.sf select')].map((s) => s.value)
  return { sels, name: document.querySelector('form.sf input.sf-input')?.value }
})
check('编辑：重复方式还原成每月', () => assert.equal(restored.sels[0], 'monthly'))
check('编辑：日期还原成 28 号', () => assert.equal(restored.sels[1], '28'))
check('编辑：时间还原成 09:05', () => {
  assert.equal(restored.sels[2], '9')
  assert.equal(restored.sels[3], '5')
})
check('编辑：名字带进表单', () => assert.equal(restored.name, 'E2E每月'))
await win.locator('form.sf .mbtn').filter({ hasText: '取消' }).click()
await win.waitForSelector('form.sf', { state: 'detached', timeout: 5_000 })

// 列对齐：任务卡和推荐项共用 .sched-grid，两边解析出来的列必须逐列相同。
// 早先写成 minmax()+fr 时，每个 grid 容器按自己的内容独立解析 fr，
// 量出来是 136/150/160/234 对 297/170/170/43 —— 只有最外侧边缘看着齐。
const grid = await win.evaluate(() => {
  const cols = (sel) => {
    const el = document.querySelector(sel)
    return el ? getComputedStyle(el).gridTemplateColumns : null
  }
  const right = (sel) => {
    const r = document.querySelector(sel)?.getBoundingClientRect()
    return r && +r.right.toFixed(0)
  }
  const ops = document.querySelector('.sched-ops')
  return {
    card: cols('.schedule-card'), preset: cols('.sched-preset'),
    cardRight: right('.schedule-card .schedule-rerun'),
    presetRight: right('.sched-preset .mbtn'),
    opsOverflow: ops ? ops.scrollWidth > ops.clientWidth + 1 : null
  }
})
check('任务区与推荐区列宽逐列相同', () => {
  assert.ok(grid.card, '任务卡没渲染出来')
  assert.ok(grid.preset, '推荐项没渲染出来')
  assert.equal(grid.preset, grid.card)
})
check('两区按钮右边缘同线', () => assert.equal(grid.presetRight, grid.cardRight))
check('操作区按钮不溢出', () => assert.equal(grid.opsOverflow, false))

await app.close()
write(failures ? `\n${failures} 处不符合预期` : '\n全部通过')
process.exit(failures ? 1 : 0)
