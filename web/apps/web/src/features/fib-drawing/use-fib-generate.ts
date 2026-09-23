import { useState } from 'react'
import type { StockSearchController } from '@/components/stock-search-box'
import { buildFibChartView, type FibChartView } from '@/lib/fib-drawing'
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
  const [view, setView] = useState<FibChartView | null>(null)

  async function generate() {
    const raw = search.symbol.trim()
    if (!raw) {
      setError(t('fib.needSymbol'))
      return
    }
    setLoading(true)
    setError('')
    const result = await buildFibChartView(raw, search.selectedStock)
    setLoading(false)
    if (!result.ok) {
      setView(null)
      setError(t(REASON_KEY[result.reason]))
      return
    }
    setView(result.view)
  }

  return { loading, error, view, generate, clearError: () => setError('') }
}
