import { useRef, useState } from 'react'
import type { StockSearchController } from '@/components/stock-search-box'
import { buildFibChartView, type FibChartView, type FibPreset } from '@/lib/fib-drawing'
import { usePreferences, type TranslationKey } from '@/lib/preferences'

const REASON_KEY = {
  unsupported: 'fib.needAshare',
  short: 'fib.needBars',
  fetch: 'fib.fetchFailed',
} as const satisfies Record<string, TranslationKey>

export function useFibGenerate(search: StockSearchController) {
  const { t } = usePreferences()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [preset, setPreset] = useState<FibPreset>('d120')
  const [view, setView] = useState<FibChartView | null>(null)
  const viewRef = useRef(view)
  viewRef.current = view

  async function generate(nextPreset = preset) {
    const raw = search.symbol.trim()
    if (!raw) {
      setError(t('fib.needSymbol'))
      return
    }
    setLoading(true)
    setError('')
    const result = await buildFibChartView(raw, search.selectedStock, nextPreset)
    setLoading(false)
    if (!result.ok) {
      setView(null)
      setError(t(REASON_KEY[result.reason]))
      return
    }
    setView(result.view)
  }

  function selectPreset(next: FibPreset) {
    setPreset(next)
    if (next !== preset && viewRef.current) void generate(next)
  }

  return { loading, error, view, preset, generate, selectPreset, clearError: () => setError('') }
}
