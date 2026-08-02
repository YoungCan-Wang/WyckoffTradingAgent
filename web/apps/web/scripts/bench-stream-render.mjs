/**
 * Stream-render benchmark: old full-MD-every-flush vs new plain-during-stream.
 *
 * Run from web/apps/web:
 *   node scripts/bench-stream-render.mjs
 */
import { createElement } from 'react'
import { renderToString } from 'react-dom/server'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

const THROTTLE_MS = 120
const OLD_THROTTLE_MS = 50
const CHUNK_CHARS = 24
const TOKEN_ARRIVAL_MS = 8 // ~125 tokens/s character-ish stream

function buildReport(targetChars = 12_000) {
  const sections = []
  let i = 0
  while (sections.join('\n').length < targetChars) {
    i += 1
    sections.push(`## Phase 观察 ${i}

本段评估 **spring** / **LPS** 与 \`SOS\` 结构。候选代码 \`${600000 + i}\`。

| 指标 | 数值 | 解读 |
| --- | --- | --- |
| 相对量能 | ${(1.1 + (i % 7) * 0.15).toFixed(2)}x | ${i % 2 ? '放量不跌' : '缩量回踩'} |
| 波动压缩 | ${(8 + (i % 5)).toFixed(1)}% | compression |

- 关键点: 关注 ${i} 日均线附近
- 风险: 跌破区间下沿则失效
- 结论: ${i % 3 === 0 ? '起跳板待确认' : '继续观察资金行为'}

\`\`\`text
code=${600000 + i} phase=C note=benchmark-fixture-${i}
\`\`\`
`)
  }
  return sections.join('\n')
}

function chunkText(text, size) {
  const out = []
  for (let i = 0; i < text.length; i += size) out.push(text.slice(i, i + size))
  return out
}

function renderMarkdown(content) {
  return renderToString(
    createElement(ReactMarkdown, { remarkPlugins: [remarkGfm] }, content),
  )
}

function renderPlain(content) {
  return renderToString(
    createElement(
      'div',
      { 'data-stream-render': 'plain' },
      createElement('div', { className: 'whitespace-pre-wrap break-words' }, content),
    ),
  )
}

function simulateFlushes(chunks, throttleMs, arrivalMs) {
  /** @type {number[]} */
  const flushAt = []
  let buf = ''
  let lastFlush = -throttleMs
  let t = 0
  for (const chunk of chunks) {
    t += arrivalMs
    buf += chunk
    if (t - lastFlush >= throttleMs) {
      flushAt.push(buf.length)
      lastFlush = t
    }
  }
  if (!flushAt.length || flushAt[flushAt.length - 1] !== buf.length) flushAt.push(buf.length)
  return { fullText: buf, flushLengths: flushAt, streamMs: t }
}

function benchPath(name, fullText, flushLengths, mode) {
  const samples = []
  let htmlBytes = 0
  let finalMdMs = 0
  const t0 = performance.now()
  for (const len of flushLengths) {
    const content = fullText.slice(0, len)
    const s0 = performance.now()
    const html = mode === 'markdown' ? renderMarkdown(content) : renderPlain(content)
    samples.push(performance.now() - s0)
    htmlBytes = html.length
  }
  if (mode === 'plain-then-md') {
    const s0 = performance.now()
    htmlBytes = renderMarkdown(fullText).length
    finalMdMs = performance.now() - s0
    samples.push(finalMdMs)
  }
  const totalMs = performance.now() - t0
  const sorted = [...samples].sort((a, b) => a - b)
  const sum = sorted.reduce((a, b) => a + b, 0)
  const p95 = sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.95))]
  return {
    name,
    renders: samples.length,
    totalMs,
    meanMs: sum / samples.length,
    maxMs: sorted[sorted.length - 1],
    p95Ms: p95,
    htmlBytes,
    finalMdMs,
  }
}

function pct(n) {
  return `${(n * 100).toFixed(1)}%`
}

function fmt(n, digits = 2) {
  return n.toFixed(digits)
}

function printRow(row) {
  console.log(
    [
      row.name.padEnd(28),
      String(row.renders).padStart(7),
      fmt(row.totalMs).padStart(10),
      fmt(row.meanMs).padStart(9),
      fmt(row.p95Ms).padStart(8),
      fmt(row.maxMs).padStart(8),
      String(row.htmlBytes).padStart(10),
    ].join('  '),
  )
}

function main() {
  const report = buildReport(12_000)
  const chunks = chunkText(report, CHUNK_CHARS)
  const current = simulateFlushes(chunks, THROTTLE_MS, TOKEN_ARRIVAL_MS)
  const oldThrottle = simulateFlushes(chunks, OLD_THROTTLE_MS, TOKEN_ARRIVAL_MS)

  // Warmup (JIT / module caches)
  renderMarkdown(report.slice(0, 800))
  renderPlain(report.slice(0, 800))

  console.log('=== Stream render bench (react-markdown SSR proxy for UI cost) ===')
  console.log(`chars=${report.length} chunks=${chunks.length} chunkSize=${CHUNK_CHARS}`)
  console.log(`tokenArrival=${TOKEN_ARRIVAL_MS}ms  streamWall≈${current.streamMs}ms`)
  console.log(`flushes@${OLD_THROTTLE_MS}ms=${oldThrottle.flushLengths.length}  flushes@${THROTTLE_MS}ms=${current.flushLengths.length}`)
  console.log('')
  console.log(
    [
      'path'.padEnd(28),
      'renders'.padStart(7),
      'total_ms'.padStart(10),
      'mean_ms'.padStart(9),
      'p95_ms'.padStart(8),
      'max_ms'.padStart(8),
      'html_len'.padStart(10),
    ].join('  '),
  )
  console.log('-'.repeat(90))

  const old50 = benchPath(`old: MD every flush@${OLD_THROTTLE_MS}`, report, oldThrottle.flushLengths, 'markdown')
  const old120 = benchPath(`old: MD every flush@${THROTTLE_MS}`, report, current.flushLengths, 'markdown')
  const neu = benchPath(`new: plain@${THROTTLE_MS}+final MD`, report, current.flushLengths, 'plain-then-md')
  const plainOnly = benchPath(`ref: plain every flush@${THROTTLE_MS}`, report, current.flushLengths, 'plain')

  for (const row of [old50, old120, neu, plainOnly]) printRow(row)

  console.log('')
  console.log('=== Deltas (vs old MD@50ms) ===')
  console.log(`new total_ms:     ${fmt(neu.totalMs)}  (${pct(1 - neu.totalMs / old50.totalMs)} faster than old@50)`)
  console.log(`new vs old@120:   ${pct(1 - neu.totalMs / old120.totalMs)} faster`)
  console.log(`MD parse count:   ${old50.renders} → 1 final (during stream: 0)`)
  console.log(`UI flush count:   ${old50.renders} → ${current.flushLengths.length} plain + 1 MD`)
  console.log(`old@50 max paint: ${fmt(old50.maxMs)}ms`)
  console.log(`plain stream max: ${fmt(plainOnly.maxMs)}ms`)
  console.log(`final MD only:    ${fmt(neu.finalMdMs)}ms`)
  console.log('')
  console.log('Note: Node SSR approximates React parse/render cost; browser paint/layout not included.')
}

main()
