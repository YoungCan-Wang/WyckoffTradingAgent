import { financialValueClass } from '@/lib/financial-colors'
import { formatSignedPercent } from '@/lib/format'
import type { ShadowEventRow, ShadowNavDailyRow, ShadowPositionRow } from '@/lib/shadow-ledger-api'
import type { ShadowCopy } from './copy'
import { formatMoney } from './showcase-panel'

export function ShadowLedgerPanel({
  copy,
  navDaily,
  positions,
  events,
  selectedAsOf,
  onSelectAsOf,
}: {
  copy: ShadowCopy
  navDaily: ShadowNavDailyRow[]
  positions: ShadowPositionRow[]
  events: ShadowEventRow[]
  selectedAsOf: string | null
  onSelectAsOf: (asOf: string) => void
}) {
  return (
    <section className="space-y-6">
      <OpenPositions copy={copy} positions={positions} />
      <div className="grid gap-4 xl:grid-cols-[1.15fr_0.85fr]">
        <DailyNavTable copy={copy} rows={navDaily} selectedAsOf={selectedAsOf} onSelectAsOf={onSelectAsOf} />
        <DayEvents copy={copy} events={events} selectedAsOf={selectedAsOf} />
      </div>
    </section>
  )
}

function OpenPositions({ copy, positions }: { copy: ShadowCopy; positions: ShadowPositionRow[] }) {
  return (
    <div>
      <h2 className="text-sm font-semibold">{copy.positions}</h2>
      {positions.length === 0 ? <p className="mt-3 text-sm text-muted-foreground">{copy.noPositions}</p> : (
        <div className="mt-3 overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead className="bg-muted/60 text-left text-xs text-muted-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">{copy.code}</th>
                <th className="px-3 py-2 font-medium">{copy.name}</th>
                <th className="px-3 py-2 font-medium">{copy.cost}</th>
                <th className="px-3 py-2 font-medium">{copy.last}</th>
                <th className="px-3 py-2 font-medium">{copy.netPnl}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {positions.map((row) => (
                <tr key={row.code}>
                  <td className="px-3 py-2 font-medium">{row.code}</td>
                  <td className="px-3 py-2">{row.name}</td>
                  <td className="px-3 py-2">{row.avgCost.toFixed(2)}</td>
                  <td className="px-3 py-2">{row.lastMark.toFixed(2)}</td>
                  <td className={`px-3 py-2 font-medium ${financialValueClass(row.netPnlAmount)}`}>
                    {formatMoney(row.netPnlAmount)}
                    {row.netPnlPct != null ? ` (${formatSignedPercent(row.netPnlPct, 1)})` : ''}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function DailyNavTable({
  copy,
  rows,
  selectedAsOf,
  onSelectAsOf,
}: {
  copy: ShadowCopy
  rows: ShadowNavDailyRow[]
  selectedAsOf: string | null
  onSelectAsOf: (asOf: string) => void
}) {
  return (
    <div>
      <h2 className="text-sm font-semibold">{copy.daily}</h2>
      <p className="mt-1 text-xs text-muted-foreground">{copy.pickDay}</p>
      <div className="mt-3 max-h-[420px] overflow-auto rounded-lg border border-border">
        <table className="w-full text-sm">
          <thead className="sticky top-0 bg-muted/80 text-left text-xs text-muted-foreground">
            <tr>
              <th className="px-3 py-2 font-medium">{copy.daily}</th>
              <th className="px-3 py-2 font-medium">{copy.pnlDay}</th>
              <th className="px-3 py-2 font-medium">{copy.pnlTotal}</th>
              <th className="px-3 py-2 font-medium">{copy.equity}</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {[...rows].reverse().map((row) => (
              <tr
                key={row.asOf}
                className={`cursor-pointer ${selectedAsOf === row.asOf ? 'bg-primary/10' : 'hover:bg-muted/50'}`}
                onClick={() => onSelectAsOf(row.asOf)}
              >
                <td className="px-3 py-2 font-medium">{row.asOf}</td>
                <td className={`px-3 py-2 ${financialValueClass(row.pnlDay)}`}>{formatMoney(row.pnlDay)}</td>
                <td className={`px-3 py-2 ${financialValueClass(row.pnlTotal)}`}>{formatMoney(row.pnlTotal)}</td>
                <td className="px-3 py-2">{row.equity.toLocaleString('zh-CN', { minimumFractionDigits: 2 })}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function DayEvents({
  copy,
  events,
  selectedAsOf,
}: {
  copy: ShadowCopy
  events: ShadowEventRow[]
  selectedAsOf: string | null
}) {
  const rows = selectedAsOf ? events.filter((row) => row.asOf === selectedAsOf) : []
  return (
    <div>
      <h2 className="text-sm font-semibold">{copy.events}</h2>
      <p className="mt-1 text-xs text-muted-foreground">{selectedAsOf || copy.pickDay}</p>
      {!selectedAsOf || rows.length === 0 ? (
        <p className="mt-3 text-sm text-muted-foreground">{selectedAsOf ? copy.noEvents : copy.pickDay}</p>
      ) : (
        <div className="mt-3 space-y-2">
          {rows.map((row) => (
            <article key={`${row.asOf}:${row.eventType}:${row.code}:${row.qty}`} className="rounded-lg border border-border bg-card px-3 py-3 text-sm">
              <div className="font-medium">{row.eventType} {row.code} {row.name}</div>
              <div className="mt-1 text-muted-foreground">
                {copy.price} {row.price == null ? '—' : row.price.toFixed(2)} · {copy.shares} {row.qty}
              </div>
              {row.reason && <p className="mt-2 text-xs leading-5 text-muted-foreground">{copy.reason}：{row.reason}</p>}
            </article>
          ))}
        </div>
      )}
    </div>
  )
}
