'use strict'

const { contextBridge, ipcRenderer, webUtils } = require('electron')

// 渲染端每个流都会临时订阅事件；把它们汇总到一个真正的 ipcRenderer 监听器，
// 避免首页并发请求超过 EventEmitter 默认上限而误报内存泄漏。
const eventHandlers = new Set()
const statusHandlers = new Set()
ipcRenderer.on('py:event', (_evt, payload) => {
  for (const handler of eventHandlers) handler(payload)
})
ipcRenderer.on('py:status', (_evt, payload) => {
  for (const handler of statusHandlers) handler(payload)
})

// The only surface the renderer gets. No fs, no child_process, no ipcRenderer.
contextBridge.exposeInMainWorld('wyckoff', {
  call: (method, params) => ipcRenderer.invoke('py:call', method, params),
  // Electron removed File.path from sandboxed renderers; this is the sanctioned
  // replacement. It only reads a path the user themselves dragged in — it
  // cannot enumerate or open anything.
  filePath: (file) => {
    try {
      return webUtils.getPathForFile(file)
    } catch {
      return ''
    }
  },
  // In-app browser. The view is a native WebContentsView, so the renderer can
  // only place it and issue whitelisted actions — it never gets the contents.
  /**
   * 可交互产物视图。和 browser 一样是原生 view —— 渲染层负责把几何镜像过来。
   * 页面本身没有 preload，拿不到这个通道；只有主界面能驱动它。
   */
  artifact: {
    load: (html) => ipcRenderer.invoke('artifact:load', html),
    show: (bounds) => ipcRenderer.invoke('artifact:show', bounds),
    hide: () => ipcRenderer.invoke('artifact:hide'),
    setBounds: (bounds) => ipcRenderer.invoke('artifact:bounds', bounds),
    destroy: () => ipcRenderer.invoke('artifact:destroy')
  },
  browser: {
    show: (bounds) => ipcRenderer.invoke('browser:show', bounds),
    hide: () => ipcRenderer.invoke('browser:hide'),
    setBounds: (bounds) => ipcRenderer.invoke('browser:bounds', bounds),
    run: (action, params) => ipcRenderer.invoke('browser:run', action, params)
  },
  // Export a report to PDF. { body, wrap, name }: wrap=true wraps body in the
  // print stylesheet (markdown); wrap=false uses body as a whole document (html).
  exportPdf: (payload) => ipcRenderer.invoke('pdf:export', payload),
  status: () => ipcRenderer.invoke('py:status'),
  restart: () => ipcRenderer.invoke('py:restart'),
  onEvent: (handler) => {
    eventHandlers.add(handler)
    return () => eventHandlers.delete(handler)
  },
  onStatus: (handler) => {
    statusHandlers.add(handler)
    return () => statusHandlers.delete(handler)
  }
})
