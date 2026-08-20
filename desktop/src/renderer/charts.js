'use strict'

/**
 * Dark data view: sector donut + holdings table.
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
// 环形图最多列这么多个桶；其余的并不消失，只是不单独占一段。
const TOP_BUCKETS = 5

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

function sectorDonut (grouping) {
  // 被截断时补一段「其他」：否则环形图只画到前 5 名之和，剩下一大截空白既
  // 不解释也不可点，看起来像画错了。补上之后环是满的，而每一段的百分比仍然
  // 是相对全部持仓的真实占比。
  const rest = grouping.truncated
    ? grouping.total - grouping.items.reduce((sum, item) => sum + item.weight, 0)
    : 0
  const sectors = rest > 0
    ? [...grouping.items, { name: t('charts.otherBucket'), weight: rest, isRest: true }]
    : grouping.items
  // 用全部持仓的合计，不是这 5 个的合计 —— 见 deriveSectors 里的说明。
  const total = grouping.total || sectors.reduce((sum, s) => sum + s.weight, 0) || 1
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
        stroke: sector.isRest ? MUTED : SECTOR_COLORS[index % SECTOR_COLORS.length],
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
    swatch.style.background = sector.isRest ? MUTED : SECTOR_COLORS[index % SECTOR_COLORS.length]
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
    const code = position.code || ''
    row.appendChild(el('td', 'c', code))
    row.appendChild(el('td', null, position.name || ''))
    row.appendChild(el('td', null, String(position.shares ?? '')))
    row.appendChild(el('td', null, String(position.cost_price ?? '')))
    const stop = position.stop_loss
    row.appendChild(
      stop === null || stop === undefined
        ? el('td', 'warnc', t('charts.noStop'))
        : el('td', null, String(stop))
    )
    // A holding row is the natural entry to its chart — it already carries the
    // symbol, so no picker is needed.
    if (code && window.WyckoffOpenKline) {
      row.className = 'dtbl-row'
      row.title = t('charts.openChart')
      row.onclick = () => window.WyckoffOpenKline(code)
    }
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
  const ranked = [...buckets.entries()]
    .map(([name, weight]) => ({ name, weight }))
    .sort((a, b) => b.weight - a.weight)
  // 合计必须在截断**之前**算。
  //
  // 原来是先 slice(0,5) 再对这 5 个求和，于是百分比按「前 5 名之和」算：
  // 10 只等权持仓每只显示 20%，而真实占比是 10%。在一个反复强调「不发明数字」
  // 的文件里，集中度这个数字本身是错的 —— 而且是往高报，正好会让人以为
  // 集中度风险比实际更大。
  const total = ranked.reduce((sum, item) => sum + item.weight, 0)
  return { items: ranked.slice(0, TOP_BUCKETS), hasSector, total, truncated: ranked.length > TOP_BUCKETS }
}

/**
 * Build the portfolio data view. `data` is the payload from IPC.
 */
/**
 * KPI + 图表 + 持仓表。
 *
 * options.withTable=false 时不画持仓表 —— 持仓页自己有一张可编辑的表，
 * 两张都画会出现两份数据，改了上面那张下面那张还是旧的。
 */
function renderCharts (data, options) {
  const withTable = !options || options.withTable !== false
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

  const charts = el('div', 'dcharts single')
  const right = sectorDonut(deriveSectors(positions))
  right.classList.add('c2')
  charts.appendChild(right)
  root.appendChild(charts)

  if (withTable) root.appendChild(holdingsTable(positions))
  root.appendChild(el('div', 'dnote', t('charts.note')))
  return root
}

window.WyckoffCharts = { renderCharts }
})()
