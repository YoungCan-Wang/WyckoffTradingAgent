'use strict'

/**
 * 可交互产物的宿主视图。
 *
 * 和 browser-host 是**相反**的策略：内置浏览器要访问外网、但不执行我们的代码；
 * 这里要执行模型生成的代码、但**一个字节都不许出网**。
 *
 * ## 为什么不能用 sandbox iframe
 *
 * 实测：blob iframe **继承宿主的 CSP**。主界面是 `script-src 'self'`，于是子
 * 文档里的内联脚本被拦：
 *
 *     Executing inline script violates the following Content Security Policy
 *     directive 'script-src 'self''. The action has been blocked.
 *
 * 而放宽宿主 CSP 是不能接受的 —— 那等于给整个应用开口子，得不偿失。
 * 所以可交互产物必须是一个**独立的 web contents**，有自己的 CSP 域。
 *
 * ## 为什么不起本地 HTTP server
 *
 * server 会给页面一个真实 origin（http://127.0.0.1:port）。同源之下它能做的
 * 事**更多**，而我们要的是相反方向 —— 能力越小越好。data: URL 配上独立
 * partition 已经够了，还省掉一个监听端口。
 *
 * ## 允许执行 JS 之后，风险到底是什么
 *
 * 不是「它能做动画」，而是「它能把持仓数据 fetch 出去」。所以这里的核心防线
 * 是**阻断一切网络**，而不是限制语法：
 *
 *  - 独立 partition：cookie / localStorage 与主应用和内置浏览器都不共享
 *  - onBeforeRequest 取消**所有** http(s)：外泄没有出口
 *  - `disable_non_proxied_udp`：WebRTC 走 UDP，`webRequest` 看不到它 ——
 *    少了这一条，模型 HTML 能用 STUN/TURN 把数据发出去（实测收到过 4 个 UDP
 *    包，而 webRequest 一个请求都没看到）
 *  - 无 preload：没有通往应用的桥（拿不到 window.wyckoff / require）
 *  - setWindowOpenHandler 一律 deny：不允许造一个没有这些限制的新窗口
 *  - will-navigate 只放行 data:：页面不能把自己导航到别处
 *
 * 实测确认（tests 里有对应断言）：JS 能跑、点击有效、fetch 被拦、
 * window.require 与 window.wyckoff 都是 undefined。
 */

const { WebContentsView } = require('electron')

/** 独立分区。不加 persist: —— 产物是一次性的，没有需要跨会话保留的状态。 */
const PARTITION = 'wyckoff-artifact'

/**
 * 产物页面的 CSP。
 *
 * 允许 inline script/style（模型生成的 HTML 就是内联的），但 default-src 'none'
 * 掐掉一切取数。img/font 放 data: 是为了让图表能内嵌小图标，仍然不出网。
 */
const ARTIFACT_CSP = [
  "default-src 'none'",
  "script-src 'unsafe-inline' 'unsafe-eval'",
  "style-src 'unsafe-inline'",
  'img-src data:',
  'font-src data:',
  "form-action 'none'",
  "base-uri 'none'"
].join('; ')

class ArtifactHost {
  constructor ({ window, onLog }) {
    this.window = window
    this.onLog = onLog || (() => {})
    this.view = null
    this.bounds = { x: 0, y: 0, width: 0, height: 0 }
    this.visible = false
  }

  /** 懒创建：没有可交互产物就不该有额外进程和内存开销。 */
  ensureView () {
    if (this.view) return this.view
    this.view = new WebContentsView({
      webPreferences: {
        // 无 preload —— 这是最重要的一条：页面拿不到任何通往应用的通道。
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        webSecurity: true,
        // 要交互就必须允许执行；安全性来自下面的网络阻断，不是来自禁用 JS。
        javascript: true,
        partition: PARTITION
      }
    })
    const wc = this.view.webContents

    // WebRTC 是 onBeforeRequest **管不到**的一条外泄通道。
    //
    // `webRequest` 只看 HTTP 栈；WebRTC 走 UDP，直接绕过。实测（用本机 UDP
    // 监听器当假 STUN 服务器）：不设策略时收到 **4 个包**，ICE 候选里带着本机
    // 内网 IP，而 webRequest 一个请求都没看到。也就是说持仓数据能靠 STUN/TURN
    // 发出去 —— fetch/img 那两道防线完全没覆盖这条路。
    //
    // `disable_non_proxied_udp` 是最强档：没有代理时不允许任何非代理 UDP。
    // 实测同一个探针在启用后收到 **0 个包**。
    //
    // 产物只需要展示，压根没有 P2P 需求，所以这里不存在「关了会坏什么」。
    wc.setWebRTCIPHandlingPolicy('disable_non_proxied_udp')

    // 核心防线：一切 http(s) 一律取消。
    //
    // 用 cancel 而不是白名单：产物没有任何合法的联网理由 —— 数据在生成它的
    // 时候就已经嵌进去了。留任何一个出口都等于留一条外泄通道。
    wc.session.webRequest.onBeforeRequest((details, callback) => {
      // devtools 例外，否则开发者工具自己打不开。
      if (/^devtools:/i.test(details.url)) return callback({})
      // 产物自身是 data: URL 载入的，那次请求必须放行。
      if (/^data:/i.test(details.url)) return callback({})
      this.onLog(`产物已拦截外部请求: ${details.url.slice(0, 80)}`)
      callback({ cancel: true })
    })

    // 导航层再拦一次：不把纵深防御全押在 onBeforeRequest 上。
    wc.on('will-navigate', (event, url) => {
      if (/^data:/i.test(url)) return
      event.preventDefault()
      this.onLog(`产物已拦截导航: ${String(url).slice(0, 80)}`)
    })

    // 不允许凭空造一个没有这些限制的新窗口。
    wc.setWindowOpenHandler(() => {
      this.onLog('产物已拦截弹窗')
      return { action: 'deny' }
    })

    wc.on('render-process-gone', (_e, details) => {
      this.onLog(`产物视图崩溃: ${details.reason}`)
    })

    return this.view
  }

  /**
   * 载入一份产物 HTML。
   *
   * CSP 由**我们**注入而不是信任模型输出里的那一份：模型可能不写、写错，
   * 或者故意写一个宽松的。注入在最前面 —— 浏览器取第一个生效的策略。
   */
  async load (html) {
    const view = this.ensureView()
    const doc = [
      '<!doctype html><html><head>',
      `<meta http-equiv="Content-Security-Policy" content="${ARTIFACT_CSP}">`,
      '<meta name="referrer" content="no-referrer">',
      '</head><body>',
      String(html || ''),
      '</body></html>'
    ].join('')
    await view.webContents.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(doc))
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

  /** 彻底销毁。产物是一次性的，关掉页签就该回收进程。 */
  destroy () {
    if (!this.view) return
    this.hide()
    try {
      this.view.webContents.close()
    } catch {
      /* 已经没了 */
    }
    this.view = null
  }
}

module.exports = { ArtifactHost, ARTIFACT_CSP, PARTITION }
