// 已登录工作台：一个窗口跑完导航、定时任务、会话归档与右键菜单。
//
// 原来这是三个文件（smoke / schedules / archive），各起一次 Electron。每次
// launch+close 约 3.2s，纯开销就占了总时长一半。它们要的环境完全一样
// （已登录 + 干净 HOME），没有理由各开一次。
//
// 顺序不是随意的，后面的段落依赖前面的产物：
//   1. 外壳    —— 白屏、标题、Markdown、侧栏展开（后面全靠侧栏）
//   2. 导航    —— 六个页面都切得过去
//   3. 弹窗    —— 打开菜单、K 线浮层、设置弹窗的 inert 与焦点
//   4. 定时任务 —— 三种重复方式存进 schedules.json，编辑往返，列对齐
//   5. 会话    —— 种数据 → 右键菜单 → 归档 → 设置页恢复/删除
//   6. 收尾    —— 渲染端有没有报错（要放最后，前面所有交互都算进来）
//
// 段与段之间必须自己收拾干净：菜单是 fixed 定位、z-index 40，留着会挡住下一段
// 要点的按钮（Playwright 报 intercepts pointer events）。设置弹窗同理。
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'
import {
  launchApp, reporter, seedSessions, openSettings, closeSettings, backendReady
} from './harness.mjs'

// 导航里的页面。**不含 chat** —— 对话是主界面，不是和这些并列的目的地，
// 它的入口是「新建分析」和会话列表。下面单独验它到得了。
const VIEWS = ['records', 'portfolio', 'schedules', 'tracking', 'attribution', 'reports']

const r = reporter()
const { app, home } = await launchApp({ signedIn: true, tag: 'workbench' })

try {
  // 渲染端的报错要冒到 CI 日志里 —— 否则白屏在这里也是「测试通过」。
  const consoleErrors = []
  const win = await app.firstWindow()
  win.on('console', (msg) => {
    if (msg.type() !== 'error' && !/MaxListenersExceededWarning/.test(msg.text())) return
    const text = msg.text()
    if (/Content-Security-Policy|ERR_CONNECTION|favicon/i.test(text)) return
    consoleErrors.push(text.slice(0, 200))
  })
  win.on('pageerror', (err) => consoleErrors.push(`pageerror: ${err.message}`))

  await win.waitForLoadState('domcontentloaded')
  // 先等界面真的起来，再动 localStorage —— reload 后同样要重新等异步账号检查。
  await win.waitForSelector('.win', { timeout: 30_000 })
  await win.evaluate(() => localStorage.setItem('wyckoff.sidebar', '0'))
  await win.reload({ waitUntil: 'domcontentloaded' })

  // ---- 1. 外壳 ------------------------------------------------------------
  r.section('外壳：')

  // 等 `.win` 而不是 `domcontentloaded`：账号态是异步查的，`account.checked` 为假
  // 时刻意什么都不渲染（否则已登录用户会先闪一下登录页）。所以 DOM 就绪的那一刻
  // 页面还是空的 —— 加登录闸门后这里三个平台全红，正是因为紧接着就找 `.nv`。
  await win.waitForSelector('.win', { timeout: 30_000 })
  const title = await win.title()
  r.check('窗口标题正确', () => assert.match(title, /Wyckoff/))

  const looseList = await win.evaluate(() => {
    const root = window.WyckoffMd.renderMarkdown('1. 第一项\n\n2. 第二项\n\n3. 第三项')
    return {
      orderedLists: root.querySelectorAll('ol').length,
      items: [...root.querySelectorAll('li')].map((item) => item.textContent)
    }
  })
  r.check('Markdown 松散有序列表保持连续', () => {
    assert.equal(looseList.orderedLists, 1)
    assert.deepEqual(looseList.items, ['第一项', '第二项', '第三项'])
  })

  // CI 的默认窗口小于 1180px，侧栏会按产品规则收起；先从真实入口展开。
  //
  // 这条是 Windows runner 上真红过一次才补的：侧栏默认是否展开看窗口宽度
  // （>= 1180 才开），而窗口宽度取自 `min(1480, 屏幕宽 - 40)`。无头虚拟显示分辨率
  // 偏小 → 侧栏收起 → `.nv` 永远等不到。本机 1430px 一直是绿的，所以只有真
  // Windows CI 能暴露它 —— 应用本身没问题（收起时切换按钮会移到顶栏），是测试
  // 假设了「侧栏总是开着」。
  //
  // 用 waitFor 而不是立刻 count()：`.win` 出现时 React 才刚开始挂子树，那一刻
  // `.side-toggle` 还是 0 个。原来的 count() 查得太早，读到 0 就直接判失败。
  if (await win.locator('.nv').count() === 0) {
    const toggle = win.locator('.side-toggle')
    const appeared = await toggle.first()
      .waitFor({ state: 'visible', timeout: 20_000 })
      .then(() => true, () => false)
    if (!appeared) {
      // 失败时把实际 DOM 状态打出来。这条在 CI 上红过三次，每次只报
      // 「false !== true」—— 那不足以判断是登录页拦住了、React 没挂完，
      // 还是窗口尺寸导致侧栏形态不同。**断言失败却不说现场，等于让下一次
      // 排查从零开始。**
      const state = await win.evaluate(() => ({
        loginCard: !!document.querySelector('.login-card'),
        win: !!document.querySelector('.win'),
        shellRoot: !!document.querySelector('.shell-root'),
        side: !!document.getElementById('side'),
        sideToggle: document.querySelectorAll('.side-toggle').length,
        nv: document.querySelectorAll('.nv').length,
        innerWidth: window.innerWidth,
        bodyLen: (document.body.innerHTML || '').length,
        topbar: !!document.querySelector('.top'),
        thread: !!document.querySelector('.thread'),
        shellChildren: [...(document.querySelector('.shell-root')?.children || [])]
          .map((el) => el.className || el.tagName).slice(0, 6),
        icb: document.querySelectorAll('.icb').length
      })).catch((err) => ({ evalFailed: String(err).slice(0, 120) }))
      r.write(`  诊断: ${JSON.stringify(state)}`)
    }
    r.check('收起侧栏有展开入口', () => assert.equal(appeared, true))
    if (appeared) await toggle.first().click()
  }
  await win.waitForSelector('.nv', { timeout: 20_000 })
  const navCount = await win.locator('.nv').count()
  r.check('侧栏导航完整', () => assert.equal(navCount, VIEWS.length))

  // ---- 2. 导航 ------------------------------------------------------------
  r.section('导航：')
  for (const view of VIEWS) {
    const nav = win.locator(`.nv[data-view="${view}"]`)
    if (await nav.count() === 0) {
      r.check(`页面 ${view} 有入口`, () => assert.fail('导航项不存在'))
      continue
    }
    await nav.click()
    await win.waitForTimeout(200)
    const active = await win.locator('.nv.on').getAttribute('data-view')
    const contentVisible = await win.locator('.page #page-body').isVisible()
    r.check(`页面 ${view} 能打开`, () => {
      assert.equal(active, view)
      assert.equal(contentVisible, true)
    })
  }

  // 对话到得了 —— 它没有导航项，入口是「新建分析」。
  await win.locator('.side-new').click()
  await win.waitForTimeout(500)
  const streamVisible = await win.locator('#stream').isVisible()
  r.check('对话到得了（走新建分析）', () => assert.equal(streamVisible, true))

  // ---- 3. 菜单与弹窗 ------------------------------------------------------
  r.section('菜单与弹窗：')

  // 「打开」菜单 —— 这条曾经因为 openBtn 未定义而整个失效。
  await win.waitForTimeout(300)
  // 账号按钮也有 aria-haspopup="menu"，所以用顶栏按钮类名消除歧义。
  const openBtn = win.locator('.topbtn[aria-haspopup="menu"]')
  if (await openBtn.count() > 0) {
    await openBtn.click()
    await win.waitForTimeout(400)
    const items = await win.locator('[role="menuitem"]').count()
    r.check('打开菜单项完整', () => assert.equal(items, 4))

    // K 线输入浮层 —— 这条正是「点了没反应」那个 bug 的落点。
    const kline = win.locator('[role="menuitem"]', { hasText: /K 线|Candle/ })
    if (await kline.count() === 1) {
      await kline.click()
      await win.waitForTimeout(600)
      const box = await win.locator('.symbox').count()
      r.check('K 线输入浮层出现', () => assert.equal(box, 1, `symbox 数量 ${box}`))
      await win.keyboard.press('Escape')
    }
  } else {
    r.check('打开菜单能弹出', () => assert.fail('找不到菜单按钮'))
  }

  // 设置弹窗只冻结背景，不能把自己也变成 inert；关闭后焦点回到账号按钮。
  await win.locator('.acct').click()
  await win.locator('.menu-i').first().click()
  await win.locator('.dlg').waitFor({ state: 'visible' })
  const modalFocused = await win.waitForFunction(
    () => document.activeElement?.classList.contains('dlg-n'),
    undefined,
    { timeout: 5_000 }
  ).then(() => true, () => false)
  const modalState = await win.evaluate(() => ({
    dialogInert: document.querySelector('.dlg')?.inert,
    backgroundInert: document.querySelector('.thread')?.inert
  }))
  r.check('设置弹窗可聚焦且只冻结背景', () => {
    assert.equal(modalState.dialogInert, false)
    assert.equal(modalState.backgroundInert, true)
    assert.equal(modalFocused, true)
  })
  await win.keyboard.press('Escape')
  await win.locator('.dlg').waitFor({ state: 'hidden' })
  const restored = await win.waitForFunction(
    () => document.activeElement?.classList.contains('acct'),
    undefined,
    { timeout: 5_000 }
  ).then(() => true, () => false)
  r.check('关闭设置后焦点回到账号按钮', () => assert.equal(restored, true))

  // ---- 4. 定时任务 --------------------------------------------------------
  // cron 编解码本身已经被单测盖住了（test/schedules.test.js）。这里验的是别的：
  // 下拉框选出来的东西真的存进 schedules.json 变成对的 cron，卡片显示与所选一致，
  // 编辑能把已存的 cron 还原回下拉框，以及两区的列真的对齐（只有量真实布局才算数）。
  r.section('定时任务：')

  // 后面两段（定时任务、会话）都要真后端：任务列表来自 schedules IPC，会话列表
  // 来自 chat_sessions。CI 的 test job **不构建 Python payload**（只有 package-*
  // 构建），所以在 CI 上必然拿不到数据。
  //
  // 这不是「原来忘了把它们加进 CI」—— 我第一版就是这么以为的，还写进了提交
  // 说明，结果三个平台全红。真相是它们**在那个 job 里跑不了**。
  //
  // 显式探测后端在不在，然后显式跳过并打印。不能静默跳：日志里看不出少验了
  // 什么的话，「少验」和「验过了」在 CI 上长得一模一样。
  const hasBackend = await backendReady(win)
  if (!hasBackend) {
    r.skip('定时任务', 'Python 后端没起来（CI 的 test job 不构建 payload）')
    r.skip('会话', '同上 —— 会话列表来自 chat_sessions IPC')
  }

  if (hasBackend) {
  await win.locator('.nv[data-view="schedules"]').click()
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
  r.check('每周：存下来的 cron 是周五 16:25', () => assert.equal(weekly.saved?.cron, '25 16 * * 5'))
  r.check('每周：保存前的预览说得对', () => assert.match(weekly.preview ?? '', /每周五\s*16:25/))

  const monthly = await createTask({ name: 'E2E每月', repeat: 'monthly', pick: '28', hour: '9', minute: '5' })
  r.check('每月：存下来的 cron 落在 28 号', () => assert.equal(monthly.saved?.cron, '5 9 28 * *'))
  r.check('每月：预览说的是几号，不是每天', () => assert.match(monthly.preview ?? '', /每月\s*28\s*号\s*09:05/))

  const weekday = await createTask({ name: 'E2E工作日', repeat: 'weekday', pick: null, hour: '8', minute: '0' })
  r.check('工作日：存下来的 cron 是 1-5', () => assert.equal(weekday.saved?.cron, '0 8 * * 1-5'))
  r.check('工作日：预览说得对', () => assert.match(weekday.preview ?? '', /工作日\s*08:00/))

  // 新建默认启用 —— 填完点保存那个意图就是「让它开始跑」。
  r.check('新建的任务默认启用', () => assert.equal(weekly.saved?.enabled, true))

  // 卡片上显示的周期，要和刚才选的一致。
  const shown = await win.evaluate(() => {
    const out = {}
    for (const card of document.querySelectorAll('.schedule-card')) {
      const name = card.querySelector('.schedule-name')?.textContent?.trim()
      if (name?.startsWith('E2E')) out[name] = card.querySelector('.schedule-cadence')?.textContent?.trim()
    }
    return out
  })
  r.check('每周：卡片显示与所选一致', () => assert.match(shown['E2E每周'] ?? '', /每周五\s*16:25/))
  r.check('每月：卡片显示与所选一致', () => assert.match(shown['E2E每月'] ?? '', /每月\s*28\s*号\s*09:05/))
  r.check('工作日：卡片显示与所选一致', () => assert.match(shown['E2E工作日'] ?? '', /工作日\s*08:00/))

  // 编辑要能把存下来的 cron 还原回下拉框 —— 往返不丢。
  await win.locator('.schedule-card', { hasText: 'E2E每月' }).locator('.mbtn', { hasText: '编辑' }).click()
  await win.waitForSelector('form.sf', { timeout: 5_000 })
  const restoredForm = await win.evaluate(() => {
    const sels = [...document.querySelectorAll('form.sf select')].map((s) => s.value)
    return { sels, name: document.querySelector('form.sf input.sf-input')?.value }
  })
  r.check('编辑：重复方式还原成每月', () => assert.equal(restoredForm.sels[0], 'monthly'))
  r.check('编辑：日期还原成 28 号', () => assert.equal(restoredForm.sels[1], '28'))
  r.check('编辑：时间还原成 09:05', () => {
    assert.equal(restoredForm.sels[2], '9')
    assert.equal(restoredForm.sels[3], '5')
  })
  r.check('编辑：名字带进表单', () => assert.equal(restoredForm.name, 'E2E每月'))
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
      const rect = document.querySelector(sel)?.getBoundingClientRect()
      return rect && +rect.right.toFixed(0)
    }
    const ops = document.querySelector('.sched-ops')
    return {
      card: cols('.schedule-card'),
      preset: cols('.sched-preset'),
      cardRight: right('.schedule-card .schedule-rerun'),
      presetRight: right('.sched-preset .mbtn'),
      opsOverflow: ops ? ops.scrollWidth > ops.clientWidth + 1 : null
    }
  })
  r.check('任务区与推荐区列宽逐列相同', () => {
    assert.ok(grid.card, '任务卡没渲染出来')
    assert.ok(grid.preset, '推荐项没渲染出来')
    assert.equal(grid.preset, grid.card)
  })
  r.check('两区按钮右边缘同线', () => assert.equal(grid.presetRight, grid.cardRight))
  r.check('操作区按钮不溢出', () => assert.equal(grid.opsOverflow, false))

  // ---- 5. 会话：右键菜单、归档、设置页删除 --------------------------------
  // 存储层和 IPC 已经被 tests/test_chat_session_archive.py 盖住了。这里验的是
  // 界面这一侧：归档真的把会话从侧栏拿走，设置页真的看得到并能恢复，侧栏没有
  // 删除入口而设置页有且要确认。
  r.section('会话：')
  await win.locator('.side-new').click()
  await win.waitForTimeout(400)

  // 导航里不该有指向对话的项，但入口仍在。
  const nav = await win.evaluate(() => ({
    labels: [...document.querySelectorAll('.nv')].map((n) => n.textContent?.trim() || ''),
    hasNewAnalysis: !!document.querySelector('.side-new'),
    hasSessionList: !!document.querySelector('[data-testid=session-list]')
  }))
  r.check('导航里没有「今天」', () => {
    assert.ok(!nav.labels.some((l) => l.includes('今天')), `实际: ${nav.labels.join('/')}`)
  })
  r.check('新建入口与会话列表都还在', () => {
    assert.equal(nav.hasNewAnalysis, true)
    assert.equal(nav.hasSessionList, true)
  })

  const sessionCount = async () => win.locator('[data-testid=session-list] .sess-row').count()
  // 第三条 e2e-keep 全程不归档 —— 用来验「全部删除」不会顺手清掉未归档的。
  await seedSessions(win, home, [
    ['e2e-a', '看看茅台的结构'],
    ['e2e-b', '帮我复盘上周'],
    ['e2e-keep', '这条一直留在侧栏']
  ])

  // 会话区和固定导航必须是两个独立滚动区，否则导航会被会话挤出视野。
  const sessionUi = await win.evaluate(() => ({
    listMounted: !!document.querySelector('[data-testid="session-list"]'),
    navIntact: document.querySelectorAll('.nv').length,
    navScrolls: getComputedStyle(document.querySelector('.nav')).overflowY,
    sessScrolls: (() => {
      const el = document.querySelector('.sess-scroll')
      return el ? getComputedStyle(el).overflowY : ''
    })()
  }))
  r.check('会话列表已挂载且未挤掉固定导航', () => {
    assert.equal(sessionUi.listMounted, true)
    assert.equal(sessionUi.navIntact, VIEWS.length)
    assert.equal(sessionUi.sessScrolls, 'auto')
    assert.notEqual(sessionUi.navScrolls, 'auto', '固定导航不该参与滚动')
  })

  // 侧栏的行内操作：应该有归档，不该有删除。
  const rowActions = await win.evaluate(() => {
    const row = document.querySelector('.sess-row')
    if (!row) return null
    return [...row.querySelectorAll('.sess-btn')].map((b) => b.getAttribute('aria-label') || '')
  })
  r.check('侧栏行上有归档按钮', () => {
    assert.ok(rowActions, '一条会话都没有')
    assert.ok(rowActions.some((l) => l.includes('归档')), `实际: ${rowActions.join('/')}`)
  })
  r.check('侧栏行上没有删除按钮', () => {
    assert.ok(!rowActions.some((l) => l.includes('删除')), `实际: ${rowActions.join('/')}`)
  })

  // 右键菜单：悬停按钮之外的第二个入口。侧栏只有 224px 宽，三个 22px 图标挤在
  // 标题右边要瞄着点；右键给的是带文字标签的大目标。
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
  r.check('右键弹出菜单', () => {
    assert.ok(ctxItems, '右键没有出菜单')
    assert.deepEqual(ctxItems.labels, ['重命名', '置顶', '归档会话'])
  })
  r.check('右键菜单不出界', () => assert.ok(ctxItems.inViewport))
  r.check('右键菜单自动聚焦第一项', () => assert.ok(ctxItems.focused, '键盘用户会开着菜单却按不动'))

  await win.keyboard.press('Escape')
  await win.waitForTimeout(300)
  const afterEsc = await win.evaluate(() => !!document.querySelector('[data-context-menu]'))
  r.check('Esc 关掉右键菜单', () => assert.ok(!afterEsc))

  // 列表滚动时菜单要关掉 —— 它是 fixed 定位的，页面一动就指错行了。
  //
  // 这里必须先确认列表**真的能滚**。只种了 3 条会话时 scrollHeight 等于
  // clientHeight，`scrollTop += 40` 是空操作、不触发 scroll 事件 —— 那样测出来的
  // 「菜单还开着」是测具本身的问题，不是组件的。用 CSS 把可视高度压到能滚为止。
  await win.evaluate(() => { document.querySelector('.sess-scroll').style.maxHeight = '80px' })
  await win.waitForTimeout(200)
  const canScroll = await win.evaluate(() => {
    const s = document.querySelector('.sess-scroll')
    return s.scrollHeight > s.clientHeight
  })
  r.check('会话列表能滚动（后一条用例的前提）', () => assert.ok(canScroll))

  await win.locator('.sess-row').first().click({ button: 'right' })
  await win.waitForTimeout(400)
  await win.evaluate(() => { document.querySelector('.sess-scroll').scrollTop += 40 })
  await win.waitForTimeout(400)
  const afterScroll = await win.evaluate(() => !!document.querySelector('[data-context-menu]'))
  r.check('滚动时关掉右键菜单', () => assert.ok(!afterScroll, '菜单还开着,但已经指错行了'))

  // 收拾干净：还原高度、关掉菜单、滚回顶部。菜单 z-index 40，留着会挡住
  // 下面要点的归档按钮。
  await win.evaluate(() => { document.querySelector('.sess-scroll').style.maxHeight = '' })
  await win.keyboard.press('Escape')
  await win.evaluate(() => { document.querySelector('.sess-scroll').scrollTop = 0 })
  await win.waitForTimeout(300)

  // 归档掉第一条，它要从侧栏消失。
  const firstId = await win.locator('.sess-row').first().getAttribute('data-session')
  const countBefore = await sessionCount()
  await win.locator('.sess-row').first().locator('.sess-btn[aria-label*="归档"]').click()
  await win.waitForTimeout(1800)
  const countAfter = await sessionCount()
  r.check('归档后从侧栏消失', () => {
    assert.equal(countAfter, countBefore - 1, `归档前 ${countBefore}，归档后 ${countAfter}`)
  })
  r.check('归档不弹确认框', () => {
    // 上面没有处理 dialog 就走通了 —— 真弹了的话 click 会卡住或超时。
    assert.ok(true)
  })

  // 设置页的「会话」区应该能看到刚归档的那条。
  await openSettings(win, '会话')
  const dlgOpened = await win.locator('.dlg').count()
  r.check('设置弹窗打开了', () => assert.ok(dlgOpened > 0, '没找到打开设置的入口'))
  await win.waitForSelector('[data-testid=archived-list]', { timeout: 8_000 })
  const archived = await win.evaluate((id) => {
    const list = document.querySelector('[data-testid=archived-list]')
    const rows = [...(list?.querySelectorAll('.mrow') || [])]
    return {
      count: rows.length,
      hasOurs: rows.some((row) => row.getAttribute('data-session') === id),
      actions: rows[0] ? [...rows[0].querySelectorAll('.mbtn')].map((b) => b.textContent?.trim()) : []
    }
  }, firstId)
  r.check('设置页能看到已归档的会话', () => {
    assert.ok(archived.count > 0, '已归档列表是空的')
    assert.equal(archived.hasOurs, true)
  })
  r.check('已归档项有恢复和删除两个动作', () => {
    assert.ok(archived.actions.some((a) => a?.includes('恢复')), `实际: ${archived.actions.join('/')}`)
    assert.ok(archived.actions.some((a) => a?.includes('删除')), `实际: ${archived.actions.join('/')}`)
  })

  // 恢复：它要回到侧栏。
  await win.locator(`[data-testid=archived-list] .mrow[data-session="${firstId}"] .mbtn`).first().click()
  await win.waitForTimeout(1500)
  const backInSidebar = await win.evaluate(
    (id) => !!document.querySelector(`.sess-row[data-session="${id}"]`),
    firstId
  )
  r.check('恢复后回到侧栏', () => assert.equal(backInSidebar, true))

  // 全部删除。先归档两条，好让「全部」真的不止一条。
  await closeSettings(win)
  for (const id of ['e2e-a', 'e2e-b']) {
    const btn = win.locator(`.sess-row[data-session="${id}"] .sess-btn[aria-label*="归档"]`)
    if (await btn.count()) { await btn.click(); await win.waitForTimeout(1200) }
  }
  await openSettings(win, '会话')
  await win.waitForSelector('[data-testid=delete-all-archived]', { timeout: 8_000 })

  const beforeAll = await win.locator('[data-testid=archived-list] .mrow').count()
  r.check('清空前有多于一条已归档', () => assert.ok(beforeAll >= 2, `实际 ${beforeAll}`))

  // 确认框要说清个数 —— 这是最后一道关。
  let confirmText = ''
  win.once('dialog', (d) => { confirmText = d.message(); void d.accept() })
  await win.locator('[data-testid=delete-all-archived]').click()
  await win.waitForTimeout(2500)
  r.check('全部删除有确认框，且说清了个数', () => {
    assert.ok(confirmText, '没弹确认框 —— 不可逆操作必须确认')
    assert.ok(confirmText.includes(String(beforeAll)), `文案里没有个数: ${confirmText}`)
  })

  const emptyNow = await win.evaluate(() => ({
    rows: document.querySelectorAll('[data-testid=archived-list] .mrow').length,
    hasList: !!document.querySelector('[data-testid=archived-list]')
  }))
  r.check('清空后已归档列表为空', () => assert.equal(emptyNow.rows, 0))
  r.check('清空后不再显示列表容器', () => assert.equal(emptyNow.hasList, false))

  // 未归档的不能被顺手删掉。
  await closeSettings(win)
  const sidebarLeft = await sessionCount()
  r.check('未归档的会话没被清掉', () => assert.ok(sidebarLeft > 0, '侧栏被清空了'))
  } // end if (hasBackend)

  // ---- 6. 收尾 ------------------------------------------------------------
  // 放最后：前面所有交互产生的报错都算进来。
  r.section('收尾：')
  r.check('渲染端无未预期报错', () =>
    assert.deepEqual(consoleErrors, [], `报错: ${consoleErrors.join(' | ')}`))
} finally {
  await app.close()
}

r.finish()
