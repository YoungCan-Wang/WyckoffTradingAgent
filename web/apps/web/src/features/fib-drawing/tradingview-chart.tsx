import { useEffect, useRef } from 'react'
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
    script.text = JSON.stringify(widgetConfig(symbol, theme, locale))
    root.append(widget, script)
    return () => {
      root.replaceChildren()
    }
  }, [symbol, theme, locale])

  return <div ref={rootRef} className="tradingview-widget-container h-full w-full" />
}

function widgetConfig(symbol: string, theme: ThemeMode, locale: Locale) {
  return {
    autosize: true,
    symbol,
    interval: 'D',
    timezone: 'Asia/Shanghai',
    theme: theme === 'dark' ? 'dark' : 'light',
    style: '1',
    locale: locale === 'zh-CN' ? 'zh_CN' : 'en',
    range: '6M',
    hide_side_toolbar: true,
    allow_symbol_change: false,
    save_image: false,
    calendar: false,
    withdateranges: false,
    support_host: 'https://www.tradingview.com',
  }
}
