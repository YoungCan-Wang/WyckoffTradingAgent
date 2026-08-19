/**
 * React 挂载点。迁移期间与 vanilla 共存：app.js 仍拥有窗口、对话流和其余
 * 面板，只有已迁移的屏交给 React。
 *
 * 暴露 window.WyckoffReact.mountSettings(host, section) 供 app.js 调用。
 * root 缓存在 host 上并复用 —— 每次打开设置都 createRoot 会泄漏 root，
 * 表现为切换几次后事件被处理多次。
 */
import type { ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { SettingsPanel } from './components/SettingsPanel'
import { TrackingPage } from './components/TrackingPage'
import { AttributionPage } from './components/AttributionPage'
import { PortfolioPage } from './components/PortfolioPage'

const roots = new WeakMap<Element, Root>()

/**
 * app.js 传进来的回调。系统消息要进对话流、退出登录要刷新侧栏并关掉面板，
 * 这些 DOM 还不属于 React，所以由它继续拥有。
 */
export interface SettingsHooks {
  onMessage: (text: string, isError?: boolean) => void
  onSignOut: () => void
  /** 配置变了要通知 app.js：输入框的模型选择器还在它手里。 */
  onConfigChanged: () => void
}

let hooks: SettingsHooks = {
  onMessage: () => {},
  onSignOut: () => {},
  onConfigChanged: () => {}
}

function setHooks (next: Partial<SettingsHooks>) {
  hooks = { ...hooks, ...next }
}

function mountSettings (host: HTMLElement, section: string) {
  let root = roots.get(host)
  if (!root) {
    root = createRoot(host)
    roots.set(host, root)
  }
  root.render(
    <SettingsPanel
      section={section}
      onMessage={hooks.onMessage}
      onSignOut={hooks.onSignOut}
      onConfigChanged={hooks.onConfigChanged}
    />
  )
}

/** 交还给 vanilla 渲染前必须卸载，否则 React 和手写 DOM 会抢同一个容器。 */
function unmountSettings (host: HTMLElement) {
  const root = roots.get(host)
  if (root) {
    root.unmount()
    roots.delete(host)
  }
}

/**
 * 挂载一个整页视图，返回 app.js 的 PAGES 约定形状 `{ node, dispose }`。
 *
 * 与设置面板不同：这里每次都新建容器和 root，由 showPage 通过 dispose 回收。
 * 复用 root 反而危险 —— 页面切换会换掉整个容器节点，而旧 root 仍指向已被
 * replaceChildren 摘掉的 DOM，下一次 render 会报 NotFoundError。
 *
 * dispose 里延后一帧卸载：unmount 不能在 React 自己的渲染周期内同步调用，
 * 否则 React 会警告「正在渲染时卸载」。
 */
function mountPage (element: ReactNode): { node: HTMLElement; dispose: () => void } {
  const node = document.createElement('div')
  node.className = 'react-page'
  const root = createRoot(node)
  root.render(element)
  return {
    node,
    dispose: () => { setTimeout(() => root.unmount(), 0) }
  }
}

declare global {
  interface Window {
    WyckoffReact: {
      mountSettings: typeof mountSettings
      unmountSettings: typeof unmountSettings
      /** app.js 用它判断某个 section 是否已经迁到 React。 */
      handles: (section: string) => boolean
      /** app.js 注入它仍拥有的动作（系统消息、退出登录）。 */
      setHooks: typeof setHooks
      /** 整页视图：返回 PAGES 约定的 { node, dispose }。 */
      trackingPage: () => { node: HTMLElement; dispose: () => void }
      attributionPage: () => { node: HTMLElement; dispose: () => void }
      portfolioPage: () => { node: HTMLElement; dispose: () => void }
    }
  }
}

// 设置面板三页全部迁完。等对话流等其余界面也搬完，这个集合和 app.js 里的
// 分派分支就可以一起删掉。
const MIGRATED = new Set(['general', 'agent', 'account'])

window.WyckoffReact = {
  mountSettings,
  unmountSettings,
  setHooks,
  handles: (section: string) => MIGRATED.has(section),
  trackingPage: () => mountPage(<TrackingPage />),
  attributionPage: () => mountPage(<AttributionPage />),
  portfolioPage: () => mountPage(<PortfolioPage />)
}
