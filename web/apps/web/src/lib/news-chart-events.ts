import { useEffect, useState } from 'react'
import type { NewsChartEvent } from '@wyckoff/shared'

interface NewsEventsState {
  events: NewsChartEvent[]
  status: 'idle' | 'loading' | 'ready' | 'error'
}

export function useNewsChartEvents(symbol: string | undefined, sessionDates: string[], name = ''): NewsEventsState {
  const [state, setState] = useState<NewsEventsState>({ events: [], status: 'idle' })
  const start = sessionDates[0] || ''
  const end = sessionDates.at(-1) || ''
  const sessions = sessionDates.join(',')

  useEffect(() => {
    if (!symbol || !/^\d{6}$/.test(symbol) || !start || !end) {
      setState({ events: [], status: 'idle' })
      return
    }
    const controller = new AbortController()
    setState((current) => ({ ...current, status: 'loading' }))
    const query = new URLSearchParams({ code: symbol, start, end, sessions })
    if (name) query.set('name', name)
    void fetch(`/api/news-events?${query}`, { signal: controller.signal })
      .then(async (response) => {
        const payload = await response.json() as { events?: NewsChartEvent[] }
        if (!response.ok) throw new Error('news_overlay_failed')
        setState({ events: Array.isArray(payload.events) ? payload.events : [], status: 'ready' })
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return
        setState({ events: [], status: error instanceof DOMException && error.name === 'AbortError' ? 'idle' : 'error' })
      })
    return () => controller.abort()
  }, [symbol, name, start, end, sessions])

  return state
}
