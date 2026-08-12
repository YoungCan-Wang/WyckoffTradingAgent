'use strict'

/**
 * Dark data view: nav curve + sector donut + holdings table.
 *
 * A-share convention throughout: RED is up, GREEN is down. The reference kit
 * used the US convention (green up); following it here would invert every
 * number a Chinese trader reads.
 */

// Wrapped: plain <script> files share one global scope, so top-level `const el`
// in several renderer files would collide.
;(function () {
const t = (key, params) => window.WyckoffI18n.t(key, params)
const NS = 'http://www.w3.org/2000/svg'

const UP = '#e5484d'
const DOWN = '#2f9e5f'
const MUTED = '#5a6472'
const WARN = '#e0a458'
const SECTOR_COLORS = [UP, WARN, '#4a90d9', DOWN, MUTED]

const el = (tag, cls, text) => {
  const node = document.createElement(tag)
  if (cls) node.className = cls
  if (text !== undefined) node.textContent = text
  return node
}

const svg = (tag, attrs) => {
  const node = document.createElementNS(NS, tag)
  for (const [key, value] of Object.entries(attrs || {})) {
    node.setAttribute(key, String(value))
  }
  return node
}

const fmtMoney = (value) =>
  `¥${Number(value || 0).toLocaleString('zh-CN', { maximumFractionDigits: 0 })}`

const fmtPct = (value) => `${value >= 0 ? '+' : ''}${Number(value || 0).toFixed(2)}%`

const signClass = (value) => (value >= 0 ? 'up' : 'dn')

/** Curve path from a series of values, scaled to the viewBox. */
function linePath (values, box) {
  if (!values.length) return ''
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const stepX = (box.w - box.left - 8) / Math.max(values.length - 1, 1)
  return values
    .map((value, index) => {
      const x = box.left + index * stepX
      const y = box.top + (1 - (value - min) / span) * box.h
      return `${index === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
}

function card(title, note) {
  const node = el('div', 'dcard')
  const head = el('div', 'h')
  head.appendChild(el('span', 'bar'))
  head.appendChild(el('b', null, title))
  if (note) head.appendChild(el('span', 'rt', note))
  node.appendChild(head)
  return node
}

function kpi (label, value, meta, cls) {
  const node = el('div', 'k')
  node.appendChild(el('div', 't', label))
  node.appendChild(el('div', `v ${cls || ''}`.trim(), value))
  if (meta) node.appendChild(el('div', `m ${cls || ''}`.trim(), meta))
  return node
}

/**
 * Placeholder until a daily-nav endpoint exists. Plotting per-position cost
 * basis here would look like a time series while being nothing of the kind —
 * a chart that invites a wrong reading is worse than an honest gap.
 */
function navPlaceholder () {
  const node = card(t('charts.equityTitle'), t('charts.equitySubPending'))
  const box = el('div', 'dph')
  box.appendChild(el('div', 'dph-t', t('charts.equityEmpty')))
  box.appendChild(el('div', 'dph-s', t('charts.equityHint')))
  node.appendChild(box)
  return node
}

function navChart (series, benchmark) {
  const node = card(t('charts.equityTitle'), t('charts.equitySubReady'))
  const box = { w: 560, h: 150, top: 18, left: 42 }
  const chart = svg('svg', { viewBox: `0 0 ${box.w} 190`, class: 'chart' })

  for (let i = 0; i <= 4; i += 1) {
    const y = box.top + (box.h / 4) * i
    chart.appendChild(svg('line', { x1: box.left, y1: y, x2: box.w - 8, y2: y, stroke: 'var(--d-line)' }))
  }

  if (benchmark && benchmark.length) {
    chart.appendChild(
      svg('path', {
        d: linePath(benchmark, box),
        fill: 'none',
        stroke: MUTED,
        'stroke-width': 1.6,
        'stroke-dasharray': '4 3'
      })
    )
  }
  chart.appendChild(
    svg('path', { d: linePath(series, box), fill: 'none', stroke: UP, 'stroke-width': 2 })
  )
  node.appendChild(chart)

  const legend = el('div', 'lg')
  for (const [color, label] of [[UP, t('charts.legendPortfolio')], [MUTED, t('charts.legendBenchmark')]]) {
    const row = el('div')
    const swatch = el('i')
    swatch.style.background = color
    row.append(swatch, document.createTextNode(label))
    legend.appendChild(row)
  }
  node.appendChild(legend)
  return node
}

function sectorDonut (grouping) {
  const sectors = grouping.items
  const total = sectors.reduce((sum, s) => sum + s.weight, 0) || 1
  const node = grouping.hasSector
    ? card(t('charts.sectorTitle'), t('charts.sectorSub', { count: sectors.length }))
    : card(t('charts.concentrationTitle'), t('charts.concentrationSub', { count: sectors.length }))
  const chart = svg('svg', { viewBox: '0 0 200 190', class: 'chart' })
  const group = svg('g', { transform: 'translate(100,88)', fill: 'none', 'stroke-width': 21 })

  const radius = 58
  const circumference = 2 * Math.PI * radius
  let offset = -90

  sectors.forEach((sector, index) => {
    const fraction = sector.weight / total
    const dash = circumference * fraction
    group.appendChild(
      svg('circle', {
        r: radius,
        stroke: SECTOR_COLORS[index % SECTOR_COLORS.length],
        'stroke-dasharray': `${dash.toFixed(1)} ${(circumference - dash).toFixed(1)}`,
        transform: `rotate(${offset.toFixed(1)})`
      })
    )
    offset += fraction * 360
  })
  chart.appendChild(group)

  const top = sectors[0]
  if (top) {
    const pct = svg('text', {
      x: 100, y: 84, fill: 'var(--d-tx)', 'font-size': 19, 'font-weight': 600,
      'text-anchor': 'middle', 'font-family': 'ui-monospace, Menlo, monospace'
    })
    pct.textContent = `${((top.weight / total) * 100).toFixed(1)}%`
    chart.appendChild(pct)
    const label = svg('text', {
      x: 100, y: 100, fill: 'var(--d-tx3)', 'font-size': 10, 'text-anchor': 'middle'
    })
    label.textContent = t('charts.weightLabel', { name: top.name })
    chart.appendChild(label)
  }
  node.appendChild(chart)

  const legend = el('div', 'lg')
  sectors.forEach((sector, index) => {
    const row = el('div')
    const swatch = el('i')
    swatch.style.background = SECTOR_COLORS[index % SECTOR_COLORS.length]
    row.appendChild(swatch)
    row.appendChild(document.createTextNode(`${sector.name} `))
    row.appendChild(el('b', null, `${((sector.weight / total) * 100).toFixed(1)}%`))
    legend.appendChild(row)
  })
  node.appendChild(legend)
  return node
}

function holdingsTable (positions) {
  const wrap = el('div', 'dtbl')
  const table = el('table')
  const head = el('tr')
  for (const label of [t('charts.colCode'), t('charts.colName'), t('charts.colShares'), t('charts.colCost'), t('charts.colStop')]) {
    head.appendChild(el('th', null, label))
  }
  table.appendChild(head)

  for (const position of positions) {
    const row = el('tr')
    row.appendChild(el('td', 'c', position.code || ''))
    row.appendChild(el('td', null, position.name || ''))
    row.appendChild(el('td', null, String(position.shares ?? '')))
    row.appendChild(el('td', null, String(position.cost_price ?? '')))
    const stop = position.stop_loss
    row.appendChild(
      stop === null || stop === undefined
        ? el('td', 'warnc', t('charts.noStop'))
        : el('td', null, String(stop))
    )
    table.appendChild(row)
  }
  wrap.appendChild(table)
  return wrap
}

/**
 * Group positions by sector when the payload carries it, otherwise by symbol.
 * Reports which grouping was used so the card can label itself honestly —
 * calling per-symbol weights "行业分布" would invent a classification.
 */
function deriveSectors (positions) {
  const hasSector = positions.some((p) => String(p.sector || '').trim())
  const buckets = new Map()
  for (const position of positions) {
    const key = hasSector
      ? String(position.sector || '').trim() || t('charts.uncategorized')
      : position.name || position.code || t('charts.unknown')
    const value = Number(position.shares || 0) * Number(position.cost_price || 0)
    buckets.set(key, (buckets.get(key) || 0) + value)
  }
  const items = [...buckets.entries()]
    .map(([name, weight]) => ({ name, weight }))
    .sort((a, b) => b.weight - a.weight)
    .slice(0, 5)
  return { items, hasSector }
}

/**
 * Build the portfolio data view. `data` is the payload from IPC.
 */
function renderCharts (data) {
  const root = el('div', 'dview')
  const positions = (data && data.positions) || []

  if (!positions.length) {
    root.appendChild(el('p', 'dempty', t('charts.empty')))
    return root
  }

  // Cost basis, not market value — there are no live quotes in this payload.
  const costBasis = positions.reduce(
    (sum, p) => sum + Number(p.shares || 0) * Number(p.cost_price || 0),
    0
  )
  const cash = Number((data && data.free_cash) || 0)
  const equity = data && data.total_equity ? Number(data.total_equity) : null
  const missingStops = positions.filter((p) => p.stop_loss === null || p.stop_loss === undefined).length

  const kpis = el('div', 'kpis')
  kpis.append(
    kpi(t('charts.kpiEquity'), equity === null ? '—' : fmtMoney(equity),
      equity === null ? t('charts.kpiUnvalued') : t('charts.kpiHolding', { count: positions.length })),
    // Labelled as cost, so no percentage change is implied.
    kpi(t('charts.kpiCost'), fmtMoney(costBasis), t('charts.kpiCount', { count: positions.length })),
    kpi(t('charts.kpiCash'), fmtMoney(cash), ''),
    // Missing stops is a risk count, not a price move: warn colour, not green/red.
    kpi(t('charts.kpiNoStop'), String(missingStops), missingStops ? t('charts.kpiNeedStop') : t('charts.kpiAllSet'),
      missingStops ? 'wn' : null)
  )
  root.appendChild(kpis)

  const charts = el('div', 'dcharts')
  const left = navPlaceholder()
  left.classList.add('c1')
  const right = sectorDonut(deriveSectors(positions))
  right.classList.add('c2')
  charts.append(left, right)
  root.appendChild(charts)

  root.appendChild(holdingsTable(positions))
  root.appendChild(el('div', 'dnote', t('charts.note')))
  return root
}

window.WyckoffCharts = { renderCharts }
})()
