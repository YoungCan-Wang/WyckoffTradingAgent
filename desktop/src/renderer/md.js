'use strict'

/**
 * Minimal markdown -> DOM for research reports.
 *
 * Everything is built with createElement/textContent. Report bodies are
 * model-generated and may quote arbitrary web text, so innerHTML is never used
 * anywhere in this file — a stray <img onerror> in a report must render as
 * literal characters, not execute.
 */

;(function () {
const RE = {
  h1: /^#\s+(.*)$/,
  h2: /^##\s+(.*)$/,
  h3: /^###\s+(.*)$/,
  quote: /^>\s?(.*)$/,
  li: /^[-*]\s+(.*)$/,
  ol: /^\d+\.\s+(.*)$/,
  fence: /^```/,
  tableSep: /^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$/,
  // A-share convention: red is up, green is down.
  pos: /^\+|^▲/,
  neg: /^-|^▼/
}

const el = (tag, cls, text) => {
  const node = document.createElement(tag)
  if (cls) node.className = cls
  if (text !== undefined) node.textContent = text
  return node
}

/** Inline: `code`, **bold**, *em*. Emits text nodes and simple elements only. */
function inline (target, raw) {
  const text = String(raw ?? '')
  const pattern = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*)/g
  let last = 0
  let match

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) {
      target.appendChild(document.createTextNode(text.slice(last, match.index)))
    }
    const token = match[0]
    if (token.startsWith('`')) {
      target.appendChild(el('code', null, token.slice(1, -1)))
    } else if (token.startsWith('**')) {
      target.appendChild(el('strong', null, token.slice(2, -2)))
    } else {
      target.appendChild(el('em', null, token.slice(1, -1)))
    }
    last = pattern.lastIndex
  }
  if (last < text.length) {
    target.appendChild(document.createTextNode(text.slice(last)))
  }
  return target
}

const splitRow = (line) =>
  line
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((cell) => cell.trim())

const isCode = (cell) => /^[0-9]{6}(\.[A-Z]{2})?$/.test(cell) || /^[A-Z]{1,5}\.[A-Z]{2}$/.test(cell)

function numericClass (cell) {
  if (RE.pos.test(cell)) return 'pos'
  if (RE.neg.test(cell)) return 'neg'
  return null
}

function buildTable (lines, start) {
  const header = splitRow(lines[start])
  let index = start + 2
  const table = el('table')
  const headRow = el('tr')
  for (const cell of header) headRow.appendChild(inline(el('th'), cell))
  table.appendChild(headRow)

  while (index < lines.length && lines[index].includes('|')) {
    const cells = splitRow(lines[index])
    const row = el('tr')
    for (const cell of cells) {
      const td = el('td', [isCode(cell) ? 'c' : null, numericClass(cell)].filter(Boolean).join(' ') || null)
      inline(td, cell)
      row.appendChild(td)
    }
    table.appendChild(row)
    index += 1
  }
  return { node: table, next: index }
}

function buildList (lines, start, ordered) {
  const list = el(ordered ? 'ol' : 'ul')
  let index = start
  const re = ordered ? RE.ol : RE.li
  while (index < lines.length) {
    const match = lines[index].match(re)
    if (!match) {
      // 模型常输出 loose list（每个序号之间空一行）；空行后的同类项仍属于
      // 同一个列表，不能拆成三个都从 1 开始的 <ol>。
      if (!lines[index].trim() && lines[index + 1]?.match(re)) {
        index += 1
        continue
      }
      break
    }
    inline(list.appendChild(el('li')), match[1])
    index += 1
  }
  return { node: list, next: index }
}

function buildQuote (lines, start) {
  const parts = []
  let index = start
  while (index < lines.length) {
    const match = lines[index].match(RE.quote)
    if (!match) break
    parts.push(match[1])
    index += 1
  }
  const quote = el('div', 'quote')
  inline(quote, parts.join(' '))
  return { node: quote, next: index }
}

function buildFence (lines, start) {
  let index = start + 1
  const body = []
  while (index < lines.length && !RE.fence.test(lines[index])) {
    body.push(lines[index])
    index += 1
  }
  const pre = el('pre')
  pre.appendChild(el('code', null, body.join('\n')))
  return { node: pre, next: index + 1 }
}

/**
 * Render markdown into a fresh element. Caller decides where to attach it.
 */
function renderMarkdown (source, meta) {
  const root = el('div', 'doc')
  const lines = String(source ?? '').replace(/\r\n/g, '\n').split('\n')
  let i = 0
  let firstHeadingDone = false

  while (i < lines.length) {
    const line = lines[i]

    if (!line.trim()) { i += 1; continue }

    if (RE.fence.test(line)) {
      const { node, next } = buildFence(lines, i)
      root.appendChild(node)
      i = next
      continue
    }

    const h1 = line.match(RE.h1)
    if (h1) {
      root.appendChild(inline(el('h1'), h1[1]))
      // The meta line belongs right under the title, like the report mockup.
      if (!firstHeadingDone && meta) {
        root.appendChild(el('div', 'meta', meta))
        firstHeadingDone = true
      }
      i += 1
      continue
    }

    const h2 = line.match(RE.h2)
    if (h2) { root.appendChild(inline(el('h2'), h2[1])); i += 1; continue }

    const h3 = line.match(RE.h3)
    if (h3) { root.appendChild(inline(el('h3'), h3[1])); i += 1; continue }

    if (RE.quote.test(line)) {
      const { node, next } = buildQuote(lines, i)
      root.appendChild(node)
      i = next
      continue
    }

    if (line.includes('|') && i + 1 < lines.length && RE.tableSep.test(lines[i + 1])) {
      const { node, next } = buildTable(lines, i)
      root.appendChild(node)
      i = next
      continue
    }

    if (RE.li.test(line)) {
      const { node, next } = buildList(lines, i, false)
      root.appendChild(node)
      i = next
      continue
    }

    if (RE.ol.test(line)) {
      const { node, next } = buildList(lines, i, true)
      root.appendChild(node)
      i = next
      continue
    }

    // Paragraph: absorb following non-structural lines.
    const buffer = [line]
    let j = i + 1
    while (j < lines.length && lines[j].trim() && !isStructural(lines[j])) {
      buffer.push(lines[j])
      j += 1
    }
    inline(root.appendChild(el('p')), buffer.join(' '))
    i = j
  }
  return root
}

function isStructural (line) {
  return (
    RE.h1.test(line) || RE.h2.test(line) || RE.h3.test(line) ||
    RE.quote.test(line) || RE.li.test(line) || RE.ol.test(line) ||
    RE.fence.test(line) || line.includes('|')
  )
}

window.WyckoffMd = { renderMarkdown }
})()
