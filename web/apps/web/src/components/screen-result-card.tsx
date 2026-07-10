import { memo, useState } from 'react'
import { Link } from 'react-router'
import { BellPlus, ChevronRight } from 'lucide-react'
import { formatStrategyPolicyText, type ScreenResult, type ScreenStockItem } from '@wyckoff/shared'
import { financialValueClass } from '@/lib/financial-colors'

type ScreenStrategyPolicy = NonNullable<ScreenResult['strategy_policy']>

function StockRow({ s, onPinStock }: { s: ScreenStockItem; onPinStock?: (stock: ScreenStockItem) => void }) {
  const chgColor = financialValueClass(s.change_pct)
  return (
    <div className="flex items-center gap-2 rounded px-2 py-1 text-xs transition-colors hover:bg-muted/60">
      <Link to={`/analysis?code=${s.code}`} className="flex min-w-0 flex-1 items-center gap-3">
        <span className="font-mono w-14 shrink-0">{s.code}</span>
        <span className="flex-1 truncate">{s.name}</span>
        <span className="w-10 text-right text-muted-foreground">{s.funnel_score?.toFixed(2) ?? '--'}</span>
        <span className={`w-16 text-right ${chgColor}`}>
          {s.change_pct != null ? `${s.change_pct >= 0 ? '+' : ''}${s.change_pct.toFixed(2)}%` : '--'}
        </span>
      </Link>
      {onPinStock && (
        <button
          type="button"
          onClick={() => onPinStock(s)}
          aria-label={`观察 ${s.code}`}
          className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-background hover:text-foreground"
        >
          <BellPlus size={13} />
        </button>
      )}
    </div>
  )
}

function StockGroup({ title, stocks, onPinStock }: { title: string; stocks: ScreenStockItem[]; onPinStock?: (stock: ScreenStockItem) => void }) {
  const [open, setOpen] = useState(true)
  return (
    <div className="mb-1.5">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1 text-xs font-medium text-muted-foreground hover:text-foreground"
      >
        <ChevronRight size={12} className={`transition-transform ${open ? 'rotate-90' : ''}`} />
        {title} ({stocks.length})
      </button>
      {open && <div className="ml-3 mt-0.5">{stocks.map(s => <StockRow key={s.code} s={s} onPinStock={onPinStock} />)}</div>}
    </div>
  )
}

export const ScreenResultCard = memo(function ScreenResultCard({ data, onPinStock }: { data: ScreenResult; onPinStock?: (stock: ScreenStockItem) => void }) {
  if (!data.stocks || data.stocks.length === 0) return null

  const highScore = data.stocks.filter(s => (s.funnel_score ?? 0) >= 0.8)
  const rest = data.stocks.filter(s => (s.funnel_score ?? 0) < 0.8)

  return (
    <div className="my-2 rounded-xl border border-border bg-card/50 p-3 text-sm shadow-sm">
      <div className="mb-2 flex items-center justify-between text-xs text-muted-foreground">
        <span>漏斗筛选 {data.date}</span>
        <span className="rounded-full bg-primary/10 px-2 py-0.5 font-medium text-primary">
          {data.meta.ai_count} 只入选
        </span>
      </div>
      <StrategyPolicyLine policy={data.strategy_policy} />
      <div className="mb-1 flex gap-4 text-[10px] text-muted-foreground px-2">
        <span className="w-14">代码</span>
        <span className="flex-1">名称</span>
        <span className="w-10 text-right">分数</span>
        <span className="w-16 text-right">涨跌</span>
      </div>
      {highScore.length > 0 && <StockGroup title="高分候选 ≥0.8" stocks={highScore} onPinStock={onPinStock} />}
      {rest.length > 0 && <StockGroup title="其他候选" stocks={rest} onPinStock={onPinStock} />}
    </div>
  )
})

function StrategyPolicyLine({ policy }: { policy?: ScreenStrategyPolicy | null }) {
  const text = formatStrategyPolicyText(policy)
  if (!text) return null
  return (
    <div className="mb-2 px-2 text-[11px] leading-relaxed text-amber-700 dark:text-amber-200">
      <span className="font-medium">策略治理：</span>
      <span className="break-words">{text}</span>
    </div>
  )
}
