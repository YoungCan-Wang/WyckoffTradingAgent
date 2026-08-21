/**
 * 把外观设置落到 <html> 的 class / CSS 变量上。
 *
 * shell.js 与设置预览都会调用，所以保持为不绑定 React 生命周期的纯函数。
 */
import type { Settings } from '../types'

const root = document.documentElement
const osDark = window.matchMedia('(prefers-color-scheme: dark)')

export function applyAppearance (cfg: Partial<Settings> | null) {
  const c = cfg || {}
  const mode = c.desktop_appearance || 'system'
  const dark = mode === 'dark' || (mode === 'system' && osDark.matches)
  root.classList.toggle('dark', dark)
  root.classList.toggle('serif', (c.desktop_font_family || 'sans') === 'serif')
  root.classList.toggle('compact', (c.desktop_density || 'cozy') === 'compact')
  root.classList.toggle('no-motion', Boolean(c.desktop_reduce_motion))
  const scale = Number(c.desktop_font_scale) || 100
  root.style.setProperty('--fs', `${(13.5 * scale) / 100}px`)
}

/** 字号拖动时的即时预览：只改 CSS 变量，不落盘。 */
export function previewFontScale (scale: number) {
  root.style.setProperty('--fs', `${(13.5 * scale) / 100}px`)
}
