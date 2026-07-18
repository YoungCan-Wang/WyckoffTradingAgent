import { BellPlus, Pin, Trash2 } from 'lucide-react'

import { formatSignedPercent } from '@/lib/format'

import type { MarketWatchSnapshot, MarketWatchQuote, WatchItem } from './types'

export function WatchlistPanelView({
  watchlist,
  marketWatch,
  onRemove,
  onStart,
}: {
  watchlist: WatchItem[]
  marketWatch: MarketWatchSnapshot | null
  onRemove: (code: string) => void
  onStart: (value: string) => void
}) {
  return (
    <div className="mx-auto w-full max-w-5xl pb-6">
      <WatchlistPanel
        watchlist={watchlist}
        marketWatch={marketWatch}
        watchlistPrompt={buildWatchlistReviewPrompt(watchlist)}
        onRemove={onRemove}
        onStart={onStart}
      />
    </div>
  )
}

function WatchlistPanel({
  watchlist,
  marketWatch,
  watchlistPrompt,
  onRemove,
  onStart,
}: {
  watchlist: WatchItem[]
  marketWatch: MarketWatchSnapshot | null
  watchlistPrompt: string
  onRemove: (code: string) => void
  onStart: (value: string) => void
}) {
  return (
    <section className="rounded-lg border border-border bg-card p-4 shadow-sm">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold">观察篮</h3>
          <p className="mt-1 text-xs text-muted-foreground">从候选、诊断和策略卡里沉淀标的。</p>
        </div>
        <button
          type="button"
          onClick={() => onStart(watchlistPrompt)}
          disabled={watchlist.length === 0}
          className="inline-flex shrink-0 items-center gap-1 rounded-md border border-border bg-background px-2.5 py-1.5 text-xs text-muted-foreground hover:bg-muted/60 hover:text-foreground disabled:cursor-not-allowed disabled:opacity-45"
        >
          <Pin size={13} />
          复盘观察篮
        </button>
      </div>
      <WatchlistBody watchlist={watchlist} marketWatch={marketWatch} onRemove={onRemove} onStart={onStart} />
    </section>
  )
}

function WatchlistBody({
  watchlist,
  marketWatch,
  onRemove,
  onStart,
}: {
  watchlist: WatchItem[]
  marketWatch: MarketWatchSnapshot | null
  onRemove: (code: string) => void
  onStart: (value: string) => void
}) {
  if (watchlist.length === 0) return <EmptyWatchlist />
  return (
    <div className="space-y-3">
      {marketWatch && <MarketWatchSummary snapshot={marketWatch} />}
      <div className="max-h-[360px] space-y-2 overflow-auto pr-1">
        {watchlist.map((item) => (
          <WatchItemCard key={item.id} item={item} quote={findQuote(item.code, marketWatch)} onRemove={onRemove} onStart={onStart} />
        ))}
      </div>
    </div>
  )
}

function EmptyWatchlist() {
  return (
    <div className="flex min-h-[260px] flex-col items-center justify-center rounded-lg border border-dashed border-border/75 bg-background px-4 text-center">
      <div className="rounded-full bg-muted p-3 text-muted-foreground">
        <BellPlus size={22} />
      </div>
      <p className="mt-3 text-sm font-medium">还没有观察标的</p>
      <p className="mt-1 max-w-[260px] text-xs leading-5 text-muted-foreground">漏斗选股、个股诊断和策略建议会提供“观察”按钮。</p>
    </div>
  )
}

function WatchItemCard({
  item,
  quote,
  onRemove,
  onStart,
}: {
  item: WatchItem
  quote: MarketWatchQuote | null
  onRemove: (code: string) => void
  onStart: (value: string) => void
}) {
  return (
    <div className="rounded-lg border border-border bg-background p-3">
      <div className="flex items-start justify-between gap-2">
        <button type="button" onClick={() => onStart(buildStockReviewPrompt(item))} className="min-w-0 text-left">
          <div className="flex min-w-0 items-center gap-2">
            <span className="font-mono text-sm font-semibold">{item.code}</span>
            {item.name && <span className="truncate text-sm font-medium">{item.name}</span>}
          </div>
          <p className="mt-1 line-clamp-2 text-xs leading-5 text-muted-foreground">{item.reason}</p>
        </button>
        <button
          type="button"
          onClick={() => onRemove(item.code)}
          aria-label={`移除 ${item.code}`}
          className="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
        >
          <Trash2 size={13} />
        </button>
      </div>
      <LiveQuote quote={quote} code={item.code} />
      <WatchItemBadges item={item} />
      <WatchItemRules item={item} />
    </div>
  )
}

function MarketWatchSummary({ snapshot }: { snapshot: MarketWatchSnapshot }) {
  const readyCount = snapshot.quotes.filter((quote) => quote.price != null || quote.changePct != null).length
  const tone = snapshot.state === 'ready'
    ? 'border-emerald-200 bg-emerald-50/70 text-emerald-900 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-100'
    : 'border-amber-200 bg-amber-50/70 text-amber-900 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-100'
  return (
    <div className={`rounded-md border px-3 py-2 text-xs ${tone}`}>
      <div className="flex items-center justify-between gap-3">
        <span className="font-medium">临时行情 · {snapshot.state === 'ready' ? `${readyCount}/${snapshot.requestedCodes.length} 个已更新` : '暂不可用'}</span>
        <span className="text-[11px] opacity-75">{formatWatchTime(snapshot.fetchedAt)}</span>
      </div>
      <p className="mt-1 opacity-80">{snapshot.state === 'ready' ? `${snapshot.fromCache ? '命中浏览器缓存' : '刚从 TickFlow 更新'}，本轮已带入模型，不写入数据库。` : snapshot.message || '发起一次读盘后刷新。'}</p>
    </div>
  )
}

function LiveQuote({ quote, code }: { quote: MarketWatchQuote | null; code: string }) {
  if (!quote) return null
  const hasPrice = quote.price != null
  const hasChange = quote.changePct != null
  return (
    <div className="mt-2 flex items-center justify-between gap-2 rounded-md bg-primary/5 px-2 py-1.5 text-xs">
      <span className="text-muted-foreground">最新可用行情</span>
      <span className="flex items-center gap-2 font-medium">
        <span>{hasPrice ? `${isCnCode(code) ? '¥' : ''}${quote.price!.toFixed(2)}` : '暂无报价'}</span>
        {hasChange && <span className={quote.changePct! >= 0 ? 'text-up' : 'text-down'}>{formatSignedPercent(quote.changePct!)}</span>}
        <span className="text-[10px] font-normal text-muted-foreground">{formatWatchTime(quote.asOf)}</span>
      </span>
    </div>
  )
}

function findQuote(code: string, snapshot: MarketWatchSnapshot | null): MarketWatchQuote | null {
  return snapshot?.quotes.find((quote) => quote.requestedCode === code) || null
}

function isCnCode(code: string): boolean {
  return /^\d{6}(?:\.(?:SH|SZ|BJ))?$/.test(code)
}

function formatWatchTime(value: string | null): string {
  if (!value) return '时间未知'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return `${date.getHours()}:${String(date.getMinutes()).padStart(2, '0')}`
}

function WatchItemBadges({ item }: { item: WatchItem }) {
  return (
    <div className="mt-2 flex flex-wrap gap-1.5 text-[11px] text-muted-foreground">
      <span className="rounded-full bg-muted px-2 py-0.5">{item.source}</span>
      {item.phase && <span className="rounded-full bg-muted px-2 py-0.5">{item.phase}</span>}
      {item.score != null && <span className="rounded-full bg-muted px-2 py-0.5">分数 {item.score.toFixed(2)}</span>}
      {item.changePct != null && (
        <span className={`rounded-full px-2 py-0.5 ${item.changePct >= 0 ? 'bg-up/10 text-up' : 'bg-down/10 text-down'}`}>
          {formatSignedPercent(item.changePct)}
        </span>
      )}
      <span className="rounded-full bg-muted px-2 py-0.5">{formatWatchDate(item.updatedAt)}</span>
    </div>
  )
}

function WatchItemRules({ item }: { item: WatchItem }) {
  return (
    <div className="mt-2 grid gap-1.5 text-[11px] sm:grid-cols-2">
      <div className="rounded-md bg-muted/45 px-2 py-1">
        <div className="text-muted-foreground">触发</div>
        <div className="mt-0.5 line-clamp-2 text-foreground">{item.trigger}</div>
      </div>
      <div className="rounded-md bg-muted/45 px-2 py-1">
        <div className="text-muted-foreground">失效</div>
        <div className="mt-0.5 line-clamp-2 text-foreground">{item.invalidation}</div>
      </div>
    </div>
  )
}

function buildWatchlistReviewPrompt(items: WatchItem[]): string {
  if (items.length === 0) return '帮我先运行漏斗选股，生成一个值得观察的股票清单。'
  const lines = items
    .slice(0, 10)
    .map((item) => `${item.code}${item.name ? ` ${item.name}` : ''}：${item.reason}；触发=${item.trigger}；失效=${item.invalidation}`)
  return `复盘我的读盘室观察篮，按优先级排序并给出今天怎么盯：\n${lines.join('\n')}`
}

function buildStockReviewPrompt(item: WatchItem): string {
  return `重点读一下 ${item.code}${item.name ? ` ${item.name}` : ''}：来源=${item.source}，观察理由=${item.reason}，触发条件=${item.trigger}，失效条件=${item.invalidation}。请结合最新市场水温和个股数据判断现在是继续观察、等待确认、试仓还是回避。`
}

function formatWatchDate(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '刚刚更新'
  return `${date.getMonth() + 1}/${date.getDate()} ${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
}
