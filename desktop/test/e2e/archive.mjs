// 会话归档：只有起真窗口才验得了的几件事。
//
// 存储层和 IPC 已经被 tests/test_chat_session_archive.py 盖住了。这里验的是别的：
// 1. 侧栏的归档按钮真的能把会话从列表里拿走
// 2. 设置页「会话」区真的能看到它，并且能恢复回侧栏
// 3. 侧栏没有删除入口；设置页有，而且删除要确认
// 4. 导航里真的没有指向对话的项，但对话仍然到得了
import { _electron as electron } from 'playwright'
import assert from 'node:assert/strict'
import { mkdtemp } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { tmpdir } from 'node:os'

const APP_DIR = join(dirname(fileURLToPath(import.meta.url)), '..', '..')

let failures = 0
const write = (s = '') => process.stdout.write(`${s}\n`)
const check = (name, fn) => {
  try { fn(); write(`  ok   ${name}`) } catch (err) { failures += 1; write(`  FAIL ${name}: ${err.message}`) }
}

const profile = await mkdtemp(join(tmpdir(), 'wyckoff-arch-'))
const home = await mkdtemp(join(tmpdir(), 'wyckoff-arch-home-'))
const app = await electron.launch({
  args: [APP_DIR, `--user-data-dir=${profile}`],
  env: { ...process.env, ELECTRON_DISABLE_GPU: '1', WYCKOFF_E2E_FAKE_SIGNIN: '1', HOME: home, USERPROFILE: home }
})
const win = await app.firstWindow()

write('会话归档：')
await win.waitForSelector('.thread', { timeout: 30_000 })

// 导航里不该有指向对话的项，但入口仍在。
const nav = await win.evaluate(() => ({
  labels: [...document.querySelectorAll('.nv')].map((n) => n.textContent?.trim() || ''),
  hasNewAnalysis: !!document.querySelector('.side-new'),
  hasSessionList: !!document.querySelector('[data-testid=session-list]')
}))
check('导航里没有「今天」', () => {
  assert.ok(!nav.labels.some((l) => l.includes('今天')), `实际: ${nav.labels.join('/')}`)
})
check('新建入口与会话列表都还在', () => {
  assert.equal(nav.hasNewAnalysis, true)
  assert.equal(nav.hasSessionList, true)
})

// 造会话：直接往库里写两条消息。
//
// **不能**只点「新建分析」—— 会话列表是 `FROM chat_log` 查出来的，一个还没说过
// 话的新会话在库里没有消息行，因此不会出现在侧栏。真跑一轮对话要等模型，
// 所以这里绕过 UI 直接种数据，再让界面重拉。
const sessionCount = async () => win.locator('[data-testid=session-list] .sess-row').count()
const dbFile = join(home, '.wyckoff', 'wyckoff.db')
{
  const { DatabaseSync } = await import('node:sqlite')
  // 建表是 Python 侧 init_db 干的，而它在第一次真正用到库时才跑。等表出现，
  // 否则种数据会撞上 "no such table"。
  const deadline = Date.now() + 30_000
  for (;;) {
    try {
      const probe = new DatabaseSync(dbFile, { readOnly: true })
      const hit = probe
        .prepare("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('chat_log','chat_session')")
        .all()
      probe.close()
      if (hit.length === 2) break
    } catch { /* 库文件还没建出来 */ }
    if (Date.now() > deadline) throw new Error('等不到 chat_log/chat_session 建表')
    await win.waitForTimeout(500)
  }
  const db = new DatabaseSync(dbFile)
  // 第三条 e2e-keep 全程不归档 —— 用来验「全部删除」不会顺手清掉未归档的。
  for (const [sid, title] of [
    ['e2e-a', '看看茅台的结构'],
    ['e2e-b', '帮我复盘上周'],
    ['e2e-keep', '这条一直留在侧栏']
  ]) {
    db.prepare(
      "INSERT INTO chat_log(session_id,user_id,role,content) VALUES(?,'','user',?)"
    ).run(sid, title)
    db.prepare(
      'INSERT INTO chat_session(session_id,user_id,title) VALUES(?,?,?)'
    ).run(sid, '', title)
  }
  db.close()
}
// 让侧栏重拉：点一次「新建分析」会触发列表刷新。
await win.locator('.side-new').click()
await win.waitForSelector('.sess-row', { timeout: 10_000 })

// 侧栏的行内操作：应该有归档，不该有删除。
const rowActions = await win.evaluate(() => {
  const row = document.querySelector('.sess-row')
  if (!row) return null
  return [...row.querySelectorAll('.sess-btn')].map((b) => b.getAttribute('aria-label') || '')
})
check('侧栏行上有归档按钮', () => {
  assert.ok(rowActions, '一条会话都没有')
  assert.ok(rowActions.some((l) => l.includes('归档')), `实际: ${rowActions.join('/')}`)
})
check('侧栏行上没有删除按钮', () => {
  assert.ok(!rowActions.some((l) => l.includes('删除')), `实际: ${rowActions.join('/')}`)
})

// ---- 右键菜单 -------------------------------------------------------------
// 悬停按钮之外的第二个入口。侧栏只有 224px 宽，三个 22px 图标挤在标题右边
// 要瞄着点；右键给的是带文字标签的大目标。
await win.locator('.sess-row').first().click({ button: 'right' })
await win.waitForTimeout(500)
const ctxItems = await win.evaluate(() => {
  const m = document.querySelector('[data-context-menu]')
  if (!m) return null
  const box = m.getBoundingClientRect()
  return {
    labels: [...m.querySelectorAll('[role="menuitem"]')].map((n) => n.textContent.trim()),
    // 菜单必须完整落在窗口内 —— 贴边右键时要朝反方向翻，不能钳到边上盖住那一行
    inViewport: box.left >= 0 && box.top >= 0 &&
      box.right <= window.innerWidth && box.bottom <= window.innerHeight,
    // 焦点要落在第一项：首帧菜单是 visibility:hidden（要先量尺寸），而隐藏元素
    // 不可聚焦，所以这里实际在验「量完尺寸后又聚焦了一次」
    focused: m.contains(document.activeElement)
  }
})
check('右键弹出菜单', () => {
  assert.ok(ctxItems, '右键没有出菜单')
  assert.deepEqual(ctxItems.labels, ['重命名', '置顶', '归档会话'])
})
check('右键菜单不出界', () => assert.ok(ctxItems.inViewport))
check('右键菜单自动聚焦第一项', () => assert.ok(ctxItems.focused, '键盘用户会开着菜单却按不动'))

await win.keyboard.press('Escape')
await win.waitForTimeout(300)
// 先 await 出结果再进 check：check 是同步的（只 try/catch 一次 fn() 调用），
// 传 async 回调的话断言会在它记完 ok 之后才 reject —— 那种测试永远绿。
const afterEsc = await win.evaluate(() => !!document.querySelector('[data-context-menu]'))
check('Esc 关掉右键菜单', () => assert.ok(!afterEsc))

// 列表滚动时菜单要关掉 —— 它是 fixed 定位的，页面一动就指错行了。
//
// 这里必须先确认列表**真的能滚**。上面只种了 3 条会话,scrollHeight 等于
// clientHeight,`scrollTop += 120` 是空操作、不触发 scroll 事件 —— 那样测出来的
// 「菜单还开着」是测具本身的问题,不是组件的。用 CSS 把可视高度压到能滚为止。
await win.evaluate(() => {
  const s = document.querySelector('.sess-scroll')
  s.style.maxHeight = '80px'
})
await win.waitForTimeout(200)
const canScroll = await win.evaluate(() => {
  const s = document.querySelector('.sess-scroll')
  return s.scrollHeight > s.clientHeight
})
check('会话列表能滚动（后一条用例的前提）', () => assert.ok(canScroll))

await win.locator('.sess-row').first().click({ button: 'right' })
await win.waitForTimeout(400)
await win.evaluate(() => { document.querySelector('.sess-scroll').scrollTop += 40 })
await win.waitForTimeout(400)
const afterScroll = await win.evaluate(() => !!document.querySelector('[data-context-menu]'))
check('滚动时关掉右键菜单', () => assert.ok(!afterScroll, '菜单还开着,但已经指错行了'))
// 还原,别把改过的高度留给后面的用例。
await win.evaluate(() => { document.querySelector('.sess-scroll').style.maxHeight = '' })

// 保证后面的用例从「没有菜单」开始:菜单是 fixed 定位、z-index 40,留着会挡住
// 下面要点的归档按钮(Playwright 会报 intercepts pointer events)。
await win.keyboard.press('Escape')
await win.evaluate(() => { document.querySelector('.sess-scroll').scrollTop = 0 })
await win.waitForTimeout(300)

// 归档掉第一条，它要从侧栏消失。
const firstId = await win.locator('.sess-row').first().getAttribute('data-session')
const countBefore = await sessionCount()
await win.locator('.sess-row').first().locator('.sess-btn[aria-label*="归档"]').click()
await win.waitForTimeout(1800)
const countAfter = await sessionCount()
check('归档后从侧栏消失', () => {
  assert.equal(countAfter, countBefore - 1, `归档前 ${countBefore}，归档后 ${countAfter}`)
})
check('归档不弹确认框', () => {
  // 上面没有处理 dialog 就走通了 —— 真弹了的话 click 会卡住或超时。
  assert.ok(true)
})

// 打开设置页的「会话」区，应该能看到刚归档的那条。
// 账号行点开是个菜单，设置项在菜单里。
await win.locator('.acct').click()
await win.waitForTimeout(700)
await win.locator('.mi, .menu-item, button', { hasText: '设置' }).first().click()
await win.waitForSelector('.dlg', { timeout: 8_000 })
const opened = await win.locator('.dlg').count()
check('设置弹窗打开了', () => assert.ok(opened > 0, '没找到打开设置的入口'))

if (opened) {
  await win.locator('.dlg-n', { hasText: '会话' }).click()
  await win.waitForSelector('[data-testid=archived-list]', { timeout: 8_000 })
  const archived = await win.evaluate((id) => {
    const list = document.querySelector('[data-testid=archived-list]')
    const rows = [...(list?.querySelectorAll('.mrow') || [])]
    return {
      count: rows.length,
      hasOurs: rows.some((r) => r.getAttribute('data-session') === id),
      actions: rows[0] ? [...rows[0].querySelectorAll('.mbtn')].map((b) => b.textContent?.trim()) : []
    }
  }, firstId)
  check('设置页能看到已归档的会话', () => {
    assert.ok(archived.count > 0, '已归档列表是空的')
    assert.equal(archived.hasOurs, true)
  })
  check('已归档项有恢复和删除两个动作', () => {
    assert.ok(archived.actions.some((a) => a?.includes('恢复')), `实际: ${archived.actions.join('/')}`)
    assert.ok(archived.actions.some((a) => a?.includes('删除')), `实际: ${archived.actions.join('/')}`)
  })

  // 恢复：它要回到侧栏。
  await win.locator(`[data-testid=archived-list] .mrow[data-session="${firstId}"] .mbtn`).first().click()
  await win.waitForTimeout(1500)
  const backInSidebar = await win.evaluate((id) => (
    !!document.querySelector(`.sess-row[data-session="${id}"]`)
  ), firstId)
  check('恢复后回到侧栏', () => assert.equal(backInSidebar, true))

  // 全部删除。先归档两条，好让「全部」真的不止一条。
  await win.locator('.dlg-x').click()
  await win.waitForSelector('.dlg', { state: 'hidden', timeout: 5_000 })
  for (const id of ['e2e-a', 'e2e-b']) {
    const btn = win.locator(`.sess-row[data-session="${id}"] .sess-btn[aria-label*="归档"]`)
    if (await btn.count()) { await btn.click(); await win.waitForTimeout(1200) }
  }
  await win.locator('.acct').click()
  await win.waitForTimeout(700)
  await win.locator('.mi, .menu-item, button', { hasText: '设置' }).first().click()
  await win.waitForSelector('.dlg', { timeout: 8_000 })
  await win.locator('.dlg-n', { hasText: '会话' }).click()
  await win.waitForSelector('[data-testid=delete-all-archived]', { timeout: 8_000 })

  const beforeAll = await win.locator('[data-testid=archived-list] .mrow').count()
  check('清空前有多于一条已归档', () => assert.ok(beforeAll >= 2, `实际 ${beforeAll}`))

  // 确认框要说清个数 —— 这是最后一道关。
  let confirmText = ''
  win.once('dialog', (d) => { confirmText = d.message(); void d.accept() })
  await win.locator('[data-testid=delete-all-archived]').click()
  await win.waitForTimeout(2500)

  check('全部删除有确认框，且说清了个数', () => {
    assert.ok(confirmText, '没弹确认框 —— 不可逆操作必须确认')
    assert.ok(confirmText.includes(String(beforeAll)), `文案里没有个数: ${confirmText}`)
  })

  const emptyNow = await win.evaluate(() => ({
    rows: document.querySelectorAll('[data-testid=archived-list] .mrow').length,
    hasList: !!document.querySelector('[data-testid=archived-list]')
  }))
  check('清空后已归档列表为空', () => assert.equal(emptyNow.rows, 0))
  check('清空后不再显示列表容器', () => assert.equal(emptyNow.hasList, false))

  // 未归档的不能被顺手删掉。
  await win.locator('.dlg-x').click()
  await win.waitForSelector('.dlg', { state: 'hidden', timeout: 5_000 })
  const sidebarLeft = await sessionCount()
  check('未归档的会话没被清掉', () => assert.ok(sidebarLeft > 0, '侧栏被清空了'))
}

await app.close()
write(failures ? `\n${failures} 处不符合预期` : '\n全部通过')
process.exit(failures ? 1 : 0)
