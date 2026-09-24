import { useEffect, useRef } from 'react'
import { AreaSeries, createChart, type Time } from 'lightweight-charts'
import { watchChartResize } from '@/lib/chart-resize'
import type { ShadowLedgerShowcase } from '@/lib/shadow-ledger-api'

export function ShadowNavChart({ curve }: { curve: ShadowLedgerShowcase['navCurve'] }) {
  const containerRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!containerRef.current || curve.length === 0) return
    const isDark = document.documentElement.classList.contains('dark')
    const up = curve[curve.length - 1]!.nav >= curve[0]!.nav
    const line = up ? '#ef4444' : '#10b981'
    const chart = createChart(containerRef.current, {
      height: 240,
      layout: {
        background: { color: isDark ? '#111827' : '#ffffff' },
        textColor: isDark ? '#9aa4b2' : '#6b7194',
        fontSize: 11,
      },
      grid: { vertLines: { visible: false }, horzLines: { color: isDark ? '#202938' : '#eef1f6' } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, fixLeftEdge: true, fixRightEdge: true },
    })
    const series = chart.addSeries(AreaSeries, {
      lineColor: line,
      topColor: up ? 'rgba(239,68,68,0.28)' : 'rgba(16,185,129,0.28)',
      bottomColor: 'transparent',
      lineWidth: 2,
      priceLineVisible: false,
    })
    series.setData(curve.map((point) => ({ time: point.asOf as Time, value: point.nav })))
    chart.timeScale().fitContent()
    const stopResize = watchChartResize(containerRef.current, chart)
    return () => {
      stopResize()
      chart.remove()
    }
  }, [curve])
  return <div ref={containerRef} className="w-full overflow-hidden rounded-lg border border-border bg-card" />
}
