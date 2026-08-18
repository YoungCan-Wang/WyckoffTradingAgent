/**
 * React 挂载点。迁移期间与 vanilla 共存：app.js 仍拥有窗口、对话流和其余
 * 面板，只有已迁移的屏交给 React。
 *
 * 暴露 window.WyckoffReact.mountSettings(host, section) 供 app.js 调用。
 * root 缓存在 host 上并复用 —— 每次打开设置都 createRoot 会泄漏 root，
 * 表现为切换几次后事件被处理多次。
 */
import { createRoot, type Root } from 'react-dom/client'
import { SettingsPanel } from './components/SettingsPanel'

const roots = new WeakMap<Element, Root>()

function mountSettings (host: HTMLElement, section: string) {
  let root = roots.get(host)
  if (!root) {
    root = createRoot(host)
    roots.set(host, root)
  }
  root.render(<SettingsPanel section={section} />)
}

/** 交还给 vanilla 渲染前必须卸载，否则 React 和手写 DOM 会抢同一个容器。 */
function unmountSettings (host: HTMLElement) {
  const root = roots.get(host)
  if (root) {
    root.unmount()
    roots.delete(host)
  }
}

declare global {
  interface Window {
    WyckoffReact: {
      mountSettings: typeof mountSettings
      unmountSettings: typeof unmountSettings
      /** app.js 用它判断某个 section 是否已经迁到 React。 */
      handles: (section: string) => boolean
    }
  }
}

const MIGRATED = new Set(['general'])

window.WyckoffReact = {
  mountSettings,
  unmountSettings,
  handles: (section: string) => MIGRATED.has(section)
}
