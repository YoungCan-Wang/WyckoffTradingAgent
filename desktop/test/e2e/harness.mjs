/**
 * e2e 公共设施：起应用、记断言、种会话。
 *
 * ## 为什么值得抽出来
 *
 * 每次 launch + close 实测约 3.2s（launch 1.3s、firstWindow 1.4s、等界面就绪
 * 0.3s、close 0.2s）。原来五个文件各起一次，33s 里有一半花在反复开关应用上。
 * 现在按「窗口能不能共用」分三份：
 *
 * - `workbench.mjs` —— 已登录工作台：导航、定时任务、会话归档与右键菜单
 * - `login.mjs`     —— 未登录形态（刻意不设 WYCKOFF_E2E_FAKE_SIGNIN）
 * - `artifact-host.mjs` —— 产物宿主安全（自建 WebContentsView 和本地 STUN）
 *
 * 后两份**不能**并进第一份：一个要的就是「没有登录标记」的启动路径，另一个
 * 要在主进程里造视图和网络设施。硬塞进已登录窗口只能靠删测例或中途重启，
 * 那等于没合。
 */
import { _electron as electron } from 'playwright'
import { mkdtemp } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { tmpdir } from 'node:os'

export const APP_DIR = join(dirname(fileURLToPath(import.meta.url)), '..', '..')

/** 计分板。每个入口文件建一个。 */
export function reporter () {
  let failures = 0
  const write = (s = '') => process.stdout.write(`${s}\n`)
  return {
    write,
    section: (title) => write(`\n${title}`),
    /**
     * 同步断言。**别传 async 回调** —— 这里只 try/catch 一次 fn() 调用，
     * 异步断言会在记完 ok 之后才 reject，那种测试永远绿。要等的东西先
     * await 出结果，再把结果拿进来断言。
     */
    check: (name, fn) => {
      try { fn(); write(`  ok   ${name}`) } catch (err) {
        failures += 1
        write(`  FAIL ${name}: ${err.message}`)
      }
    },
    get failures () { return failures },
    finish () {
      write(failures ? `\n*** ${failures} 项失败 ***` : '\n全部通过')
      process.exitCode = failures ? 1 : 0
    }
  }
}

/**
 * 起一个应用实例。
 *
 * `signedIn` 为真时设 WYCKOFF_E2E_FAKE_SIGNIN。用环境变量旁路而**不是**伪造
 * session 文件：假 token 会被 restore_session 拿去问 Supabase，判 invalid 后
 * clear_session()，CI 上稳定回到登录页。那条路查了三次才明白 —— 本机不复现
 * （网络错误走「保留 session」分支），只有 CI 命中「无效凭据」分支。
 *
 * 每个实例都给独立的 HOME：schedules.json 和 wyckoff.db 都在那儿，共用会踩到
 * 开发机上真实的定时任务和会话。
 */
export async function launchApp ({ signedIn = true, tag = 'e2e' } = {}) {
  const profile = await mkdtemp(join(tmpdir(), `wyckoff-${tag}-`))
  const home = await mkdtemp(join(tmpdir(), `wyckoff-${tag}-home-`))
  const env = { ...process.env, ELECTRON_DISABLE_GPU: '1', HOME: home, USERPROFILE: home }
  // CI 的 Windows/Linux runner 没有 GPU；不关掉会在启动阶段卡住或刷一堆警告。
  if (signedIn) env.WYCKOFF_E2E_FAKE_SIGNIN = '1'
  const app = await electron.launch({ args: [APP_DIR, `--user-data-dir=${profile}`], env })
  return { app, home, profile }
}

/**
 * 往库里直接写会话。
 *
 * **不能**只点「新建分析」：会话列表是 FROM chat_log 查出来的，一个还没说过话
 * 的新会话在库里没有消息行，不会出现在侧栏。真跑一轮对话要等模型，所以绕过
 * UI 种数据，再让界面重拉。
 *
 * 建表是 Python 侧 init_db 干的，它在第一次真正用到库时才跑 —— 所以要等表出现，
 * 否则撞 "no such table"。
 */
export async function seedSessions (win, home, rows) {
  const { DatabaseSync } = await import('node:sqlite')
  const dbFile = join(home, '.wyckoff', 'wyckoff.db')
  const deadline = Date.now() + 30_000
  for (;;) {
    let ready = false
    try {
      const probe = new DatabaseSync(dbFile, { readOnly: true })
      ready = probe
        .prepare("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('chat_log','chat_session')")
        .all().length === 2
      probe.close()
    } catch { /* 库文件还没建出来 */ }
    if (ready) break
    if (Date.now() > deadline) throw new Error('等不到 chat_log/chat_session 建表')
    await win.waitForTimeout(500)
  }
  const db = new DatabaseSync(dbFile)
  for (const [sid, title] of rows) {
    db.prepare("INSERT INTO chat_log(session_id,user_id,role,content) VALUES(?,'','user',?)").run(sid, title)
    db.prepare('INSERT INTO chat_session(session_id,user_id,title) VALUES(?,?,?)').run(sid, '', title)
  }
  db.close()
  // 让侧栏重拉：点一次「新建分析」会触发列表刷新。
  await win.locator('.side-new').click()
  await win.waitForSelector('.sess-row', { timeout: 10_000 })
}

/** 打开设置弹窗的某个分区。账号行点开是菜单，设置项在菜单里。 */
export async function openSettings (win, sectionLabel) {
  await win.locator('.acct').click()
  await win.waitForTimeout(700)
  await win.locator('.mi, .menu-item, button', { hasText: '设置' }).first().click()
  await win.waitForSelector('.dlg', { timeout: 8_000 })
  if (sectionLabel) await win.locator('.dlg-n', { hasText: sectionLabel }).click()
}

/** 关掉设置弹窗并等它真的消失 —— 留着会挡住后面要点的东西。 */
export async function closeSettings (win) {
  await win.locator('.dlg-x').click()
  await win.waitForSelector('.dlg', { state: 'hidden', timeout: 5_000 })
}
