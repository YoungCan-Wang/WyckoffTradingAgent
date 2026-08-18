'use strict'

const { app, BrowserWindow, ipcMain, dialog } = require('electron')
const path = require('node:path')
const fs = require('node:fs')
const { PythonBridge } = require('./python-bridge')
const { DaemonRunner } = require('./daemon-runner')
const { BrowserHost } = require('./browser-host')
const { PRINT_WEB_PREFERENCES, blockPrintNetwork } = require('./print-security')

// src/ -> desktop/ -> repo root
const REPO_ROOT = path.resolve(__dirname, '..', '..')

// 分发版没有仓库：Python 是打包进 resources/ 的自包含二进制，REPO_ROOT 只是
// 个占位。开发时这个断言很有价值（根目录算错的话 Python 子进程永远起不来，
// 早失败好过后面莫名其妙），但打包后必须放行 —— 否则主进程在这里就抛异常，
// 窗口起来了却什么都没初始化，而且异常被 Electron 吞掉、日志里什么都没有。
if (!app.isPackaged && !require('node:fs').existsSync(path.join(REPO_ROOT, 'cli'))) {
  throw new Error(`repo root looks wrong: ${REPO_ROOT} (no cli/ directory)`)
}

// Window geometry lives in Electron's own userData, not wyckoff.json: it is a
// pure UI concern the Python side never reads, and writing it through IPC on
// every resize would be pointless traffic.
const BOUNDS_FILE = path.join(app.getPath('userData'), 'window-state.json')

function loadBounds () {
  try {
    const raw = require('node:fs').readFileSync(BOUNDS_FILE, 'utf8')
    const b = JSON.parse(raw)
    if ([b.width, b.height, b.x, b.y].every((n) => Number.isFinite(n))) return b
  } catch {
    /* first run, or the file was corrupted — fall back to defaults */
  }
  return null
}

function saveBounds (win) {
  if (!win || win.isDestroyed() || win.isMinimized() || win.isFullScreen()) return
  try {
    require('node:fs').writeFileSync(BOUNDS_FILE, JSON.stringify(win.getBounds()), 'utf8')
  } catch {
    /* losing window position is not worth surfacing to the user */
  }
}

let mainWindow = null
let bridge = null
let daemon = null
let browser = null
// Python reaches ready in ~1s, which can beat the renderer's listener
// registration. Without replay that status is lost and the UI sits on
// "连接中…" forever, never issuing a single call.
let lastStatus = { state: 'starting' }

function createWindow () {
  // Size to the PRIMARY display's work area. A hardcoded 1480 was wider than
  // this 1470px laptop screen, and with no explicit position the window could
  // land on a secondary display — invisible if that screen is off or elsewhere.
  const { screen } = require('electron')
  const work = screen.getPrimaryDisplay().workAreaSize
  const width = Math.min(1480, work.width - 40)
  const height = Math.min(880, work.height - 40)

  // Restore the previous position only if it still intersects a connected
  // display. Otherwise an unplugged monitor leaves the window off-screen —
  // exactly the "app won't show up" symptom, but permanent.
  const saved = loadBounds()
  const onScreen = saved && screen.getAllDisplays().some((d) => {
    const w = d.workArea
    return (
      saved.x < w.x + w.width && saved.x + saved.width > w.x &&
      saved.y < w.y + w.height && saved.y + saved.height > w.y
    )
  })

  mainWindow = new BrowserWindow({
    ...(onScreen ? saved : { width, height, center: true }),
    minWidth: Math.min(1100, width),
    minHeight: Math.min(700, height),
    title: 'Wyckoff 读盘室',
    backgroundColor: '#fffefc',
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'default',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      // Renderer stays sandboxed: it can only reach Python through the
      // contextBridge surface, never spawn processes or read the filesystem.
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  })

  // Renderer errors are invisible from the terminal otherwise, which makes a
  // blank window impossible to diagnose.
  mainWindow.webContents.on('console-message', (_e, level, message, line, source) => {
    if (level >= 2) console.error(`[renderer] ${message} (${source}:${line})`)
  })
  mainWindow.webContents.on('did-fail-load', (_e, code, desc) => {
    console.error(`[renderer] load failed: ${desc} (${code})`)
  })

  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'))

  // Launched from a terminal, macOS gives the window no activation, so it opens
  // behind whatever is in front. Claim focus once the content is ready.
  mainWindow.once('ready-to-show', () => {
    mainWindow.show()
    app.focus({ steal: true })
  })

  // Debounced: a drag fires these continuously, and each one would be a write.
  let boundsTimer = null
  const rememberBounds = () => {
    clearTimeout(boundsTimer)
    boundsTimer = setTimeout(() => saveBounds(mainWindow), 400)
  }
  mainWindow.on('resize', rememberBounds)
  mainWindow.on('move', rememberBounds)

  mainWindow.on('close', () => {
    clearTimeout(boundsTimer)
    saveBounds(mainWindow)
  })

  mainWindow.on('closed', () => {
    mainWindow = null
  })
}

function forward (channel, payload) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send(channel, payload)
  }
}

function startBridge (browserEndpoint) {
  bridge = new PythonBridge({
    repoRoot: REPO_ROOT,
    browserEndpoint,
    onEvent: (event) => forward('py:event', event),
    onStatus: (status) => {
      if (status.state !== 'log') lastStatus = status
      // Technical detail is for the logs, never the user's screen.
      if (status.detail) console.error(`[bridge] ${status.detail}`)
      forward('py:status', status)
    }
  })
  bridge.start()
}

/**
 * Scheduled tasks run only while this app is open — no launchd install. The
 * daemon reuses the bridge's interpreter resolution so a worktree without its
 * own .venv still works.
 */
function startDaemon () {
  // 打包版暂不支持定时任务：daemon 走的是 `python -m cli daemon`，而分发包里
  // 只有 IPC 那个入口的二进制，没有可用的 cli 模块。明确不启动并说明原因，
  // 比让它 spawn 失败后静默重试好 —— 后者在界面上表现为「定时任务开着但从不跑」。
  if (app.isPackaged) {
    console.log('[daemon] 打包版尚未内置调度进程，定时任务需要在命令行运行 wyckoff daemon')
    return
  }
  daemon = new DaemonRunner({
    repoRoot: REPO_ROOT,
    python: bridge.pythonPath(),
    onLog: (message) => console.log(`[daemon] ${message}`)
  })
  daemon.start()
}

let printCssCache = null

const escapeHtml = (s) =>
  String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]))

/** Wrap the rendered .doc markup in the print stylesheet (read from disk once). */
async function wrapForPrint (bodyHtml, name) {
  if (printCssCache == null) {
    try {
      printCssCache = await fs.promises.readFile(path.join(__dirname, 'renderer', 'print.css'), 'utf8')
    } catch {
      printCssCache = ''
    }
  }
  return (
    '<!doctype html><html><head><meta charset="utf-8">' +
    `<title>${escapeHtml(name)}</title><style>${printCssCache}</style>` +
    `</head><body>${bodyHtml}</body></html>`
  )
}

/**
 * Render report HTML to a PDF via an offscreen window and save it where the
 * user picks. The window is fully locked down (no preload, no node, sandboxed)
 * because it loads model-generated report HTML — same trust level as the
 * viewer's iframe, just headed for print instead of screen.
 */
async function exportPdf (payload) {
  const body = String((payload && payload.body) || '')
  if (!body) return { ok: false, error: 'empty document' }
  const name = String((payload && payload.name) || 'report')
  // markdown (wrap=true): body is our .doc DOM, wrap it in the print stylesheet.
  // html (wrap=false): body is the model's full document, use it verbatim.
  const html = payload && payload.wrap ? await wrapForPrint(body, name) : body
  const defaultName = name + '.pdf'

  const target = await dialog.showSaveDialog(mainWindow, {
    defaultPath: defaultName,
    filters: [{ name: 'PDF', extensions: ['pdf'] }]
  })
  if (target.canceled || !target.filePath) return { ok: false, canceled: true }

  const win = new BrowserWindow({
    show: false,
    webPreferences: {
      ...PRINT_WEB_PREFERENCES,
      // webRequest listeners are session-wide. Keep print-only restrictions
      // away from the main renderer and the agent browser.
      partition: `print-${Date.now()}-${Math.random().toString(16).slice(2)}`
    }
  })
  blockPrintNetwork(win.webContents.session)
  try {
    // data: URL keeps it an opaque origin with no filesystem access.
    await win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(html))
    const pdf = await win.webContents.printToPDF({
      pageSize: 'A4',
      printBackground: true,
      margins: { marginType: 'default' }
    })
    await fs.promises.writeFile(target.filePath, pdf)
    return { ok: true, path: target.filePath }
  } catch (err) {
    return { ok: false, error: err.message }
  } finally {
    win.destroy()
  }
}

app.whenReady().then(async () => {
  createWindow()
  const endpoint = await createBrowserHost()
  startBridge(endpoint)
  startDaemon()

  // Renderer drives visibility and geometry; the view is not a DOM element, so
  // the layout has to be mirrored explicitly.
  ipcMain.handle('browser:show', (_evt, bounds) => {
    if (!browser) return { ok: false }
    browser.setBounds(bounds)
    browser.show()
    return { ok: true }
  })
  ipcMain.handle('browser:hide', () => {
    if (browser) browser.hide()
    return { ok: true }
  })
  ipcMain.handle('browser:bounds', (_evt, bounds) => {
    if (browser) browser.setBounds(bounds)
    return { ok: true }
  })
  ipcMain.handle('browser:run', async (_evt, action, params) => {
    if (!browser) return { ok: false, error: 'browser unavailable' }
    try {
      return { ok: true, result: await browser.run(String(action || ''), params || {}) }
    } catch (err) {
      return { ok: false, error: err.message }
    }
  })

  ipcMain.handle('py:call', (_evt, method, params) => {
    if (typeof method !== 'string' || !method) {
      return { ok: false, error: 'invalid method' }
    }
    return bridge.send(method, params)
  })

  ipcMain.handle('pdf:export', (_evt, payload) => exportPdf(payload))

  // The renderer asks for current status once its listener is attached, so a
  // ready that arrived early is not lost.
  ipcMain.handle('py:status', () => lastStatus)

  ipcMain.handle('py:restart', () => {
    // bridge.restart() recovers from every failure state, including spawn
    // errors and the "gave up" state that stop()+start() could not.
    bridge.restart()
    return { ok: true }
  })

  app.on('activate', async () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow()
      const endpoint = await createBrowserHost()
      bridge.browserEndpoint = endpoint
      // Closing the last macOS window stops all window-owned resources. Restart
      // even if the old child has not emitted exit yet; restart() detaches it.
      bridge.restart()
      if (!daemon.child) daemon.start()
    }
  })
})

async function createBrowserHost () {
  browser = new BrowserHost({
    window: mainWindow,
    onLog: (message) => console.log(`[browser] ${message}`)
  })
  try {
    return await browser.start()
  } catch (err) {
    console.error(`[browser] control endpoint failed: ${err.message}`)
    return null
  }
}

// Killing the children on every exit path is what keeps orphan Python
// processes from accumulating — the main risk of a resident-subprocess design.
app.on('window-all-closed', () => {
  if (bridge) bridge.stop()
  if (daemon) daemon.stop()
  if (browser) browser.stop()
  if (process.platform !== 'darwin') app.quit()
})

app.on('before-quit', () => {
  if (bridge) bridge.stop()
  if (daemon) daemon.stop()
  if (browser) browser.stop()
})

process.on('exit', () => {
  if (bridge) bridge.stop()
  if (daemon) daemon.stop()
  if (browser) browser.stop()
})
