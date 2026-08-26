'use strict'

// 应用内浏览器：一个 WebContentsView，Agent 通过本地控制口驱动它。
//
// 为什么不给主应用开 --remote-debugging-port：那会把 preload 桥、你的登录
// 会话、以及所有 IPC 通道一起暴露给本机任意进程。这里反过来——只开一个
// 绑定 127.0.0.1 的控制口，带一次性 token，且只接受白名单动作，
// 浏览器视图本身跑在独立 session 分区里，拿不到主窗口的任何东西。

const http = require('node:http')
const crypto = require('node:crypto')
const { WebContentsView } = require('electron')
const { validatePublicHttpUrl } = require('./public-url')

// 只允许这些动作。新增动作必须显式加进来，避免控制口变成任意代码执行。
const ACTIONS = new Set(['navigate', 'text', 'title', 'url', 'click', 'fill', 'back', 'wait', 'screenshot'])

const NAV_TIMEOUT_MS = 30_000
const ACTION_TIMEOUT_MS = 15_000
const MAX_TEXT_CHARS = 20_000

class BrowserHost {
  constructor ({ window, onLog }) {
    this.window = window
    this.onLog = onLog || (() => {})
    this.view = null
    this.server = null
    this.token = crypto.randomBytes(24).toString('hex')
    this.port = 0
    this.bounds = { x: 0, y: 0, width: 0, height: 0 }
    this.visible = false
    // 发起请求的那个 session；ensureView 建视图时赋值。校验域名要用它解析。
    this.session = null
  }

  /** 懒创建：不打开浏览器就不该有额外进程和内存开销。 */
  ensureView () {
    if (this.view) return this.view
    this.view = new WebContentsView({
      webPreferences: {
        // 无 preload：页面不该有任何通往本应用的桥。
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        webSecurity: true,
        // 独立分区：cookie/localStorage 与主应用完全隔离，登录态不串。
        partition: 'persist:agent-browser'
      }
    })
    const wc = this.view.webContents
    // 校验必须用**这个 session** 解析域名：它和随后真正发起连接的是同一个
    // host resolver，解析结果进同一份 DNS 缓存。用 Node 的 dns.lookup 校验
    // 等于「各自解析一次」，短 TTL 域名可以在两次解析之间把地址换成内网。
    this.session = wc.session
    wc.session.webRequest.onBeforeRequest((details, callback) => {
      // 只放行 http(s)。以前对其他 scheme 一律 callback({})，等于把 file:
      // about: blob: data: 全放过 —— 这个视图没有 preload、开了 sandbox，
      // 读到本地文件也没有通往应用的桥，但没有理由把这扇门留着。
      // devtools: 例外：不放行会让开发者工具自己打不开。
      if (/^(devtools|chrome-extension):/i.test(details.url)) return callback({})
      if (!/^https?:/i.test(details.url)) {
        this.onLog(`浏览器已拦截非 http(s) 请求: ${details.url.slice(0, 80)}`)
        return callback({ cancel: true })
      }
      validatePublicHttpUrl(details.url, this.session)
        .then(() => callback({}))
        .catch((err) => {
          this.onLog(`浏览器导航已拦截: ${err.message}`)
          callback({ cancel: true })
        })
    })
    // onBeforeRequest 之外再加一道：某些导航（重定向链、页面内 location 赋值）
    // 值得在导航层面也拦一次，不把纵深防御全押在 Chromium 的默认策略上。
    wc.on('will-navigate', (event, url) => {
      if (/^https?:/i.test(url)) return
      event.preventDefault()
      this.onLog(`浏览器已拦截非 http(s) 导航: ${String(url).slice(0, 80)}`)
    })
    // 页面里的 window.open / target=_blank 一律在当前视图导航，
    // 不允许它凭空造出没有这些限制的新窗口。
    wc.setWindowOpenHandler(({ url }) => {
      validatePublicHttpUrl(url, this.session)
        .then((safeUrl) => wc.loadURL(safeUrl))
        .catch((err) => this.onLog(`浏览器弹窗导航已拦截: ${err.message}`))
      return { action: 'deny' }
    })
    wc.on('render-process-gone', (_e, details) => {
      this.onLog(`浏览器视图崩溃: ${details.reason}`)
    })
    return this.view
  }

  setBounds (bounds) {
    this.bounds = bounds
    if (this.view && this.visible) this.view.setBounds(bounds)
  }

  show () {
    const view = this.ensureView()
    if (!this.visible) {
      this.window.contentView.addChildView(view)
      this.visible = true
    }
    view.setBounds(this.bounds)
  }

  hide () {
    if (!this.view || !this.visible) return
    this.window.contentView.removeChildView(this.view)
    this.visible = false
  }

  async run (action, params) {
    if (!ACTIONS.has(action)) throw new Error(`unknown action: ${action}`)
    const wc = this.ensureView().webContents

    if (action === 'navigate') {
      const url = await validatePublicHttpUrl(params.url, this.session)
      await this.withTimeout(wc.loadURL(url), NAV_TIMEOUT_MS, 'navigate timed out')
      return { url: wc.getURL(), title: wc.getTitle() }
    }

    if (action === 'back') {
      if (wc.navigationHistory.canGoBack()) wc.navigationHistory.goBack()
      return { url: wc.getURL() }
    }

    if (action === 'wait') {
      const ms = Math.min(Math.max(Number(params.ms) || 500, 50), 10_000)
      await new Promise((resolve) => setTimeout(resolve, ms))
      return { waited_ms: ms }
    }

    if (action === 'title') return { title: wc.getTitle() }
    if (action === 'url') return { url: wc.getURL() }

    if (action === 'text') {
      const text = await this.eval(wc, '(document.body && document.body.innerText) || ""')
      return { text: String(text || '').slice(0, MAX_TEXT_CHARS), url: wc.getURL() }
    }

    if (action === 'screenshot') {
      const image = await this.withTimeout(wc.capturePage(), ACTION_TIMEOUT_MS, 'screenshot timed out')
      return { png_base64: image.toPNG().toString('base64') }
    }

    // click / fill 都要选择器，用 JSON.stringify 注入避免拼接出可执行片段。
    const selector = String(params.selector || '')
    if (!selector) throw new Error('selector is required')
    const sel = JSON.stringify(selector)

    if (action === 'click') {
      const ok = await this.eval(wc, `(() => {
        const node = document.querySelector(${sel})
        if (!node) return false
        node.click()
        return true
      })()`)
      if (!ok) throw new Error(`no element matched ${selector}`)
      return { clicked: selector }
    }

    const value = JSON.stringify(String(params.value ?? ''))
    const ok = await this.eval(wc, `(() => {
      const node = document.querySelector(${sel})
      if (!node) return false
      node.value = ${value}
      node.dispatchEvent(new Event('input', { bubbles: true }))
      node.dispatchEvent(new Event('change', { bubbles: true }))
      return true
    })()`)
    if (!ok) throw new Error(`no element matched ${selector}`)
    return { filled: selector }
  }

  eval (wc, code) {
    // 从未导航过的 view 上 executeJavaScript **永不 resolve**（Electron 43 实测；
    // 38 上也一样）。withTimeout 能兜住，但那是白等 15 秒才报一句
    // 「script timed out」—— 而这个状态是当下就能知道的：还没有页面，自然没有
    // 正文可读、没有元素可点。直接说清楚。
    if (!wc.getURL()) {
      return Promise.reject(new Error('browser has no page yet — navigate first'))
    }
    // userGesture=false：不给页面借 Agent 之手触发需要用户手势的能力。
    return this.withTimeout(wc.executeJavaScript(code, false), ACTION_TIMEOUT_MS, 'script timed out')
  }

  withTimeout (promise, ms, message) {
    return Promise.race([
      promise,
      new Promise((_resolve, reject) => setTimeout(() => reject(new Error(message)), ms))
    ])
  }

  /** 启动控制口。只监听回环地址，端口由系统分配。 */
  start () {
    if (this.server) return Promise.resolve({ port: this.port, token: this.token })
    return new Promise((resolve, reject) => {
      this.server = http.createServer((req, res) => this.handle(req, res))
      this.server.on('error', reject)
      this.server.listen(0, '127.0.0.1', () => {
        this.port = this.server.address().port
        this.onLog(`浏览器控制口 127.0.0.1:${this.port}`)
        resolve({ port: this.port, token: this.token })
      })
    })
  }

  async handle (req, res) {
    const send = (code, body) => {
      res.writeHead(code, { 'content-type': 'application/json' })
      res.end(JSON.stringify(body))
    }
    // 常量时间比较，避免用响应时间猜 token。
    const supplied = String(req.headers['x-wyckoff-token'] || '')
    const expected = this.token
    const same =
      supplied.length === expected.length &&
      crypto.timingSafeEqual(Buffer.from(supplied), Buffer.from(expected))
    if (!same) return send(403, { error: 'forbidden' })
    if (req.method !== 'POST') return send(405, { error: 'method not allowed' })

    let raw = ''
    req.on('data', (chunk) => {
      raw += chunk
      // 控制指令都很小；超过就掐断，别让请求体吃满内存。
      if (raw.length > 64 * 1024) req.destroy()
    })
    req.on('end', async () => {
      let payload = {}
      try {
        payload = JSON.parse(raw || '{}')
      } catch {
        return send(400, { error: 'invalid JSON' })
      }
      try {
        const result = await this.run(String(payload.action || ''), payload.params || {})
        send(200, { ok: true, result })
      } catch (err) {
        send(200, { ok: false, error: err.message })
      }
    })
  }

  stop () {
    if (this.server) {
      this.server.close()
      this.server = null
    }
    this.hide()
    if (this.view) {
      this.view.webContents.close()
      this.view = null
    }
  }
}

module.exports = { BrowserHost }
