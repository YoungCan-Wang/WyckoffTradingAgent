import { useEffect, useRef } from 'react'
import { tradingViewWidgetOptions } from '@/lib/fib-drawing'
import type { Locale, ThemeMode } from '@/lib/preferences'

const CHART_PAINT_MS = 900

interface TradingViewChartProps {
  symbol: string
  theme: ThemeMode
  locale: Locale
  onReset: () => void
  onReady: () => void
}

export function TradingViewChart({ symbol, theme, locale, onReset, onReady }: TradingViewChartProps) {
  const rootRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const root = rootRef.current
    if (!root) return
    let cancelled = false
    let timer = 0
    onReset()
    root.replaceChildren()
    root.addEventListener('load', onFrameLoad, true)
    const widget = document.createElement('div')
    widget.className = 'tradingview-widget-container__widget'
    widget.style.height = 'calc(100% - 32px)'
    widget.style.width = '100%'
    const script = document.createElement('script')
    script.src = 'https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js'
    script.async = true
    script.text = JSON.stringify(tradingViewWidgetOptions({
      symbol,
      theme: theme === 'dark' ? 'dark' : 'light',
      locale: locale === 'zh-CN' ? 'zh_CN' : 'en',
    }))
    root.append(widget, script)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
      root.removeEventListener('load', onFrameLoad, true)
      root.replaceChildren()
    }

    function onFrameLoad(event: Event) {
      if (cancelled || !(event.target instanceof HTMLIFrameElement) || !root?.contains(event.target)) return
      window.clearTimeout(timer)
      timer = window.setTimeout(() => {
        if (!cancelled) onReady()
      }, CHART_PAINT_MS)
    }
  }, [symbol, theme, locale, onReset, onReady])

  return <div ref={rootRef} className="tradingview-widget-container h-full w-full" />
}
