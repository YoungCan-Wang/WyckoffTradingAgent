import { formatTimestampDate } from './format'

export type ExportCell = string | number
export type ExportRow = Record<string, ExportCell>

export interface ExportDataset {
  symbol: string
  fileStem: string
  rawRows: ExportRow[]
  enhancedRows: ExportRow[]
}

export type ExportAdjust = '' | 'qfq' | 'hfq'

const TEXT_ENCODER = new TextEncoder()
const CRC_TABLE = buildCrcTable()

export function normalizeExportSymbol(raw: string): string {
  const input = raw.trim().toUpperCase()
  if (/^\d{6}\.(SH|SZ|BJ)$/.test(input)) return input
  if (/^\d{6}(SH|SZ|BJ)$/.test(input)) return `${input.slice(0, 6)}.${input.slice(6)}`
  if (/^\d{5}\.HK$/.test(input) || /^[A-Z][A-Z0-9.-]{0,15}\.US$/.test(input)) return input
  if (!/^\d{6}$/.test(input)) return ''
  if (input.startsWith('0') || input.startsWith('1') || input.startsWith('2') || input.startsWith('3')) return `${input}.SZ`
  if (input.startsWith('4') || input.startsWith('8') || input.startsWith('9')) return `${input}.BJ`
  return `${input}.SH`
}

/** Resolve a typed/selected search hit into TickFlow export symbol (supports Chinese names via caller-provided stock). */
export function exportSymbolFromStock(stock: { symbol?: string; analysisCode?: string } | null | undefined): string {
  if (!stock) return ''
  return normalizeExportSymbol(stock.symbol || stock.analysisCode || '')
}

export function parseExportSymbols(text: string): string[] {
  const matches = text
    .toUpperCase()
    .match(/\d{6}(?:\.(?:SH|SZ|BJ)|(?:SH|SZ|BJ))?|\d{5}\.HK|[A-Z][A-Z0-9.-]{0,15}\.US/g) || []
  const seen = new Set<string>()
  return matches.reduce<string[]>((out, token) => {
    const symbol = normalizeExportSymbol(token)
    if (symbol && !seen.has(symbol)) {
      seen.add(symbol)
      out.push(symbol)
    }
    return out
  }, [])
}

export function buildKlineParams(symbol: string, days: number, endOffset: number, adjust: ExportAdjust): URLSearchParams {
  const endDate = new Date()
  endDate.setDate(endDate.getDate() - endOffset)
  const startDate = new Date(endDate)
  startDate.setDate(endDate.getDate() - Math.ceil(days * 1.6))
  return new URLSearchParams({
    symbol,
    period: '1d',
    count: String(days),
    adjust: adjust === 'qfq' ? 'forward' : adjust === 'hfq' ? 'backward' : 'none',
    start_time: String(toMs(formatCompactDate(startDate))),
    end_time: String(toMs(formatCompactDate(endDate))),
  })
}

export function parseTickFlowToRows(json: Record<string, unknown>): ExportRow[] {
  const data = json.data
  if (Array.isArray(data)) return normalizeRowArray(data)
  if (Array.isArray(json.records)) return normalizeRowArray(json.records)
  const table = findTickFlowTable(data)
  if (!table) return []
  const timestamps = Array.isArray(table.timestamp) ? table.timestamp : []
  const keys = Object.keys(table).filter((key) => Array.isArray(table[key]) && key !== 'timestamp')
  return timestamps.map((ts, index) => tableRowToExportRow(ts, keys, table, index))
}

export function buildEnhancedRows(rows: ExportRow[]): ExportRow[] {
  return rows.map((row) => {
    const volume = numberValue(row, ['Volume', 'volume', 'vol', '成交量'])
    const amount = numberValue(row, ['Amount', 'amount', 'turnover', '成交额'])
    return {
      Date: stringValue(row, ['Date', 'date', 'trade_date', 'timestamp']),
      Open: numberValue(row, ['Open', 'open', '开盘']),
      High: numberValue(row, ['High', 'high', '最高']),
      Low: numberValue(row, ['Low', 'low', '最低']),
      Close: numberValue(row, ['Close', 'close', '收盘']),
      Volume: volume,
      Amount: amount,
      TurnoverRate: numberValue(row, ['TurnoverRate', 'turnover_rate', 'turnoverRate', '换手率']),
      Amplitude: numberValue(row, ['Amplitude', 'amplitude', '振幅']),
      AvgPrice: typeof volume === 'number' && typeof amount === 'number' && volume > 0 && amount > 0 ? amount / volume : '',
      Sector: stringValue(row, ['Sector', 'sector', '行业']),
    }
  })
}

export function arrayToCSV(rows: Record<string, unknown>[]): string {
  if (rows.length === 0 || !rows[0]) return ''
  const headers = Object.keys(rows[0])
  const lines = [headers.join(',')]
  for (const row of rows) lines.push(headers.map((header) => csvCell(row[header])).join(','))
  return lines.join('\n')
}

export function createZipBlob(files: { name: string; content: string }[]): Blob {
  const localParts: Uint8Array[] = []
  const centralParts: Uint8Array[] = []
  let offset = 0
  for (const file of files) {
    const name = TEXT_ENCODER.encode(file.name)
    const body = TEXT_ENCODER.encode(file.content)
    const crc = crc32(body)
    localParts.push(zipLocalHeader(name, body.length, crc), body)
    centralParts.push(zipCentralHeader(name, body.length, crc, offset))
    offset += localParts.at(-2)!.length + body.length
  }
  const central = concatBytes(centralParts)
  const local = concatBytes(localParts)
  return new Blob([arrayBuffer(local), arrayBuffer(central), arrayBuffer(zipEndRecord(files.length, central.length, local.length))], { type: 'application/zip' })
}

export function downloadBlob(blob: Blob, fileName: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = fileName
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

function normalizeRowArray(rows: unknown[]): ExportRow[] {
  return rows
    .filter((row): row is Record<string, unknown> => Boolean(row) && typeof row === 'object' && !Array.isArray(row))
    .map((row) => Object.fromEntries(Object.entries(row).map(([key, value]) => [key, normalizeCell(key, value)])))
}

function findTickFlowTable(data: unknown): Record<string, unknown[]> | null {
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null
  const obj = data as Record<string, unknown>
  if (Array.isArray(obj.timestamp)) return obj as Record<string, unknown[]>
  for (const value of Object.values(obj)) {
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      const table = value as Record<string, unknown>
      if (Array.isArray(table.timestamp)) return table as Record<string, unknown[]>
    }
  }
  return null
}

function tableRowToExportRow(ts: unknown, keys: string[], table: Record<string, unknown[]>, index: number): ExportRow {
  const row: ExportRow = { date: formatTimestampDate(ts) }
  for (const key of keys) row[key] = normalizeCell(key, table[key]?.[index])
  return row
}

function normalizeCell(key: string, value: unknown): ExportCell {
  if (key === 'date' || key === 'trade_date') return String(value || '').replace(/(\d{4})(\d{2})(\d{2})/, '$1-$2-$3')
  if (key === 'timestamp') return formatTimestampDate(value)
  const numeric = Number(value)
  return Number.isFinite(numeric) && value !== '' ? numeric : String(value ?? '')
}

function numberValue(row: ExportRow, aliases: string[]): number | '' {
  const value = pickValue(row, aliases)
  const numeric = Number(value)
  return Number.isFinite(numeric) && value !== '' ? numeric : ''
}

function stringValue(row: ExportRow, aliases: string[]): string {
  return String(pickValue(row, aliases) ?? '')
}

function pickValue(row: ExportRow, aliases: string[]): ExportCell | undefined {
  for (const key of aliases) {
    if (Object.prototype.hasOwnProperty.call(row, key)) return row[key]
  }
  return undefined
}

function csvCell(value: unknown): string {
  if (value == null) return ''
  const text = String(value)
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
}

function formatCompactDate(d: Date): string {
  return d.toISOString().slice(0, 10).replace(/-/g, '')
}

function toMs(date: string): number {
  return new Date(date.replace(/(\d{4})(\d{2})(\d{2})/, '$1-$2-$3')).getTime()
}

function buildCrcTable(): Uint32Array {
  const table = new Uint32Array(256)
  for (let i = 0; i < 256; i++) {
    let c = i
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    table[i] = c >>> 0
  }
  return table
}

function crc32(bytes: Uint8Array): number {
  let crc = 0xffffffff
  for (const byte of bytes) crc = CRC_TABLE[(crc ^ byte) & 0xff]! ^ (crc >>> 8)
  return (crc ^ 0xffffffff) >>> 0
}

function zipLocalHeader(name: Uint8Array, size: number, crc: number): Uint8Array {
  return bytes([0x04034b50, 20, 0, 0, 0, 0, crc, size, size, name.length, 0], [4, 2, 2, 2, 2, 2, 4, 4, 4, 2, 2], name)
}

function zipCentralHeader(name: Uint8Array, size: number, crc: number, offset: number): Uint8Array {
  return bytes(
    [0x02014b50, 20, 20, 0, 0, 0, 0, crc, size, size, name.length, 0, 0, 0, 0, 0, offset],
    [4, 2, 2, 2, 2, 2, 2, 4, 4, 4, 2, 2, 2, 2, 2, 4, 4],
    name,
  )
}

function zipEndRecord(fileCount: number, centralSize: number, centralOffset: number): Uint8Array {
  return bytes([0x06054b50, 0, 0, fileCount, fileCount, centralSize, centralOffset, 0], [4, 2, 2, 2, 2, 4, 4, 2])
}

function bytes(values: number[], sizes: number[], tail?: Uint8Array): Uint8Array {
  const length = sizes.reduce((sum, size) => sum + size, 0) + (tail?.length || 0)
  const out = new Uint8Array(length)
  const view = new DataView(out.buffer)
  let offset = 0
  values.forEach((value, index) => {
    const size = sizes[index]!
    if (size === 2) view.setUint16(offset, value, true)
    if (size === 4) view.setUint32(offset, value, true)
    offset += size
  })
  if (tail) out.set(tail, offset)
  return out
}

function concatBytes(parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((sum, part) => sum + part.length, 0))
  let offset = 0
  for (const part of parts) {
    out.set(part, offset)
    offset += part.length
  }
  return out
}

function arrayBuffer(bytesValue: Uint8Array): ArrayBuffer {
  return bytesValue.buffer.slice(bytesValue.byteOffset, bytesValue.byteOffset + bytesValue.byteLength) as ArrayBuffer
}
