'use strict'

const { contextBridge, ipcRenderer, webUtils } = require('electron')

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
  browser: {
    show: (bounds) => ipcRenderer.invoke('browser:show', bounds),
    hide: () => ipcRenderer.invoke('browser:hide'),
    setBounds: (bounds) => ipcRenderer.invoke('browser:bounds', bounds),
    run: (action, params) => ipcRenderer.invoke('browser:run', action, params)
  },
  status: () => ipcRenderer.invoke('py:status'),
  restart: () => ipcRenderer.invoke('py:restart'),
  onEvent: (handler) => {
    const listener = (_evt, payload) => handler(payload)
    ipcRenderer.on('py:event', listener)
    return () => ipcRenderer.removeListener('py:event', listener)
  },
  onStatus: (handler) => {
    const listener = (_evt, payload) => handler(payload)
    ipcRenderer.on('py:status', listener)
    return () => ipcRenderer.removeListener('py:status', listener)
  }
})
