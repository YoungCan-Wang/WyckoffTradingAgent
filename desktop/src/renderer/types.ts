/**
 * preload.js 暴露的桥面 + Python 事件结构的类型定义。
 *
 * 这些类型是手写的，不是从 Python 生成的 —— 契约在 cli/ipc/methods.py 那边。
 * 改后端事件结构时这里要跟着改；类型不匹配只会在编译时报错，而不是运行时
 * 静默拿到 undefined。
 */

/** Python 侧回来的一条事件。type 决定还有哪些字段。 */
export interface PyEvent {
  id?: string
  type:
    | 'ready'
    | 'result'
    | 'end'
    | 'error'
    | 'text_delta'
    | 'thinking_delta'
    | 'tool_start'
    | 'tool_error'
    | 'approval_pending'
    | 'done'
  // result 事件把方法的返回值平铺在顶层，所以这里必须允许任意键。
  [key: string]: unknown
}

/** 桥的连接状态。log 只是转发日志，不改变状态机。 */
export interface PyStatus {
  state: 'starting' | 'ready' | 'error' | 'log'
  reason?: string
  detail?: string
}

export interface Bounds {
  x: number
  y: number
  width: number
  height: number
}

export interface WyckoffBridge {
  call: (method: string, params?: Record<string, unknown>) => Promise<{ ok: boolean; id?: string }>
  filePath: (file: File) => string
  browser: {
    show: (bounds?: Bounds) => Promise<unknown>
    hide: () => Promise<unknown>
    setBounds: (bounds: Bounds) => Promise<unknown>
    run: (action: string, params?: Record<string, unknown>) => Promise<{ ok: boolean; result?: unknown; error?: string }>
  }
  exportPdf: (payload: { body: string; wrap: boolean; name: string }) => Promise<{ ok: boolean; error?: string }>
  status: () => Promise<PyStatus>
  restart: () => Promise<{ ok: boolean }>
  onEvent: (handler: (event: PyEvent) => void) => () => void
  onStatus: (handler: (status: PyStatus) => void) => () => void
}

/** 设置面板读到的配置。字段名与 Python 侧 settings_get 一一对应。 */
export interface Settings {
  models: Array<{ id: string; provider?: string; role?: string }>
  default_model: string
  fallback_model: string
  stream_chunk_timeout_seconds: number
  tool_timeout_seconds: number
  has_tickflow_key: boolean
  has_tushare_token: boolean
  desktop_appearance: 'system' | 'light' | 'dark'
  desktop_font_scale: number
  desktop_font_family: 'sans' | 'serif'
  desktop_density: 'cozy' | 'compact'
  desktop_reduce_motion: boolean
  desktop_send_on_enter: boolean
  desktop_tone: string
  desktop_tone_custom: string
}

/** locales.js / i18n.js 目前还是 vanilla，先按运行时形状声明。 */
export interface I18n {
  t: (key: string, params?: Record<string, string | number>) => string
  getLang: () => string
  setLang: (lang: string, opts?: { persist?: boolean }) => void
  resolveInitial: () => string
  available: () => Array<{ code: string; label: string }>
  applyDom: (root?: ParentNode) => void
}

declare global {
  interface Window {
    wyckoff: WyckoffBridge
    WyckoffI18n: I18n
  }
}
