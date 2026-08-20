/**
 * preload.js 暴露的桥面 + Python 事件结构的类型定义。
 *
 * 这些类型是手写的，不是从 Python 生成的 —— 契约在 cli/ipc/methods.py 那边。
 * 改后端事件结构时这里要跟着改；类型不匹配只会在编译时报错，而不是运行时
 * 静默拿到 undefined。
 */

/**
 * Python 侧回来的一条事件。type 决定还有哪些字段。
 *
 * id 是**数字**：python-bridge.js 用自增计数器发号。这里以前写成 string，
 * 于是把它塞进 Set<string> 再用 String(event.id) 去查恒为假 —— 事件全被丢掉，
 * 而 tsc 因为类型撒了谎完全没报警。要比就两边都 String()。
 */
export interface PyEvent {
  id?: string | number
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
  /** ok:false 时带 error（主进程的 py:call 在方法名非法或桥未就绪时返回）。 */
  call: (
    method: string,
    params?: Record<string, unknown>
  ) => Promise<{ ok: boolean; id?: string | number; error?: string }>
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

/**
 * 一条持仓。字段与 portfolio(mode="view") 返回的完全一致 —— 只有这六个。
 *
 * 注意没有 sector：图表里的行业分布在缺它时会退化成按名称分组（charts.js
 * 的 deriveSectors），那是刻意的降级，不是缺字段的 bug。
 */
export interface Position {
  code: string
  name: string
  shares: number
  /** 云端层内部叫 cost，但视图层统一转成 cost_price，读写一致。 */
  cost_price: number
  buy_dt: string
  /** null 表示没设止损 —— 与 0 不是一回事。 */
  stop_loss: number | null
}

export interface Portfolio {
  positions: Position[]
  free_cash: number
  total_equity?: number
  position_count?: number
  /** 后端算估值的时间，不是缓存时间 —— 两者要分开显示。 */
  valuation_updated_at?: string
}

/** 一个已配置的模型。字段与 settings_get 里构造的字典一一对应。 */
export interface ModelEntry {
  id: string
  model: string
  provider_name: string
  /** 不是机密（密钥才是），用来区分同名的自建端点。 */
  base_url: string
  has_key: boolean
}

/** 设置面板读到的配置。字段名与 Python 侧 settings_get 一一对应。 */
export interface Settings {
  models: ModelEntry[]
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
    /** charts.js（vanilla）：把持仓渲染成 KPI + SVG 图表 + 表格。 */
    WyckoffCharts: {
      /** withTable:false 时不画只读持仓表（持仓页自己有可编辑的那张）。 */
      renderCharts: (portfolio: unknown, options?: { withTable?: boolean }) => HTMLElement
    }
    /** app.js 注册的「在产物面板打开 K 线」。可能尚未就绪，调用前判空。 */
    WyckoffOpenKline?: (code: string) => void
  }
}
