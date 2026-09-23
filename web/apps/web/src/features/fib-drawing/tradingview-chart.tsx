import { useEffect, useRef } from 'react'
import { tradingViewWidgetOptions } from '@/lib/fib-drawing'
import type { Locale, ThemeMode } from '@/lib/preferences'

interface TradingViewChartProps {
  symbol: string
  theme: ThemeMode
  locale: Locale
}

export function TradingViewChart({ symbol, theme, locale }: TradingViewChartProps) {
  const rootRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const root = rootRef.current
    if (!root) return
    root.replaceChildren()
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
      root.replaceChildren()
    }
  }, [symbol, theme, locale])

  return <div ref={rootRef} className="tradingview-widget-container h-full w-full" />
}
