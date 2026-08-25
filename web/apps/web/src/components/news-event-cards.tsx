import type { NewsChartEvent } from '@wyckoff/shared'
import { usePreferences } from '@/lib/preferences'

const KIND_LABELS: Record<NewsChartEvent['kind'], string> = {
  regulatory: '监管',
  risk: '风险',
  earnings: '业绩',
  holder: '股东',
  deal: '交易',
}

const SENTIMENT_LABELS: Record<NewsChartEvent['sentiment'], string> = {
  bullish: '偏多',
  bearish: '偏空',
  mixed: '混合',
  unknown: '中性',
}

export function NewsEventCards({ events, status }: { events: NewsChartEvent[]; status: 'idle' | 'loading' | 'ready' | 'error' }) {
  const { t } = usePreferences()
  if (status === 'idle') return null
  return (
    <div className="mt-3 rounded-lg border border-border bg-background px-3 py-3">
      <div className="mb-2">
        <div className="text-sm font-medium">{t('analysis.newsTitle')}</div>
        <p className="mt-1 text-xs text-muted-foreground">{t('analysis.newsSubtitle')}</p>
      </div>
      {status === 'loading' && <p className="text-xs text-muted-foreground">{t('common.loading')}</p>}
      {status === 'error' && <p className="text-xs text-muted-foreground">{t('analysis.newsUnavailable')}</p>}
      {status === 'ready' && events.length === 0 && <p className="text-xs text-muted-foreground">{t('analysis.newsEmpty')}</p>}
      {status === 'ready' && events.length > 0 && (
        <div className="grid gap-2 md:grid-cols-2">
          {events.map((event) => (
            <NewsEventCard key={`${event.date}-${event.title}`} event={event} />
          ))}
        </div>
      )}
    </div>
  )
}

function NewsEventCard({ event }: { event: NewsChartEvent }) {
  const tone = event.sentiment === 'bullish' ? 'text-red-600' : event.sentiment === 'bearish' ? 'text-emerald-600' : 'text-muted-foreground'
  return (
    <a
      href={event.url || undefined}
      target={event.url ? '_blank' : undefined}
      rel={event.url ? 'noreferrer' : undefined}
      className="rounded-md border border-border px-3 py-2 text-xs hover:bg-muted/40"
    >
      <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
        <span>{event.date}</span>
        <span>{KIND_LABELS[event.kind]}</span>
        <span className={tone}>{SENTIMENT_LABELS[event.sentiment]}</span>
      </div>
      <div className="mt-1 font-medium text-foreground">{event.title}</div>
      <p className="mt-1 text-muted-foreground">{event.summary}</p>
    </a>
  )
}
