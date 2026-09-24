import type {
  AutoscaleInfo,
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesApi,
  ISeriesPrimitive,
  ISeriesPrimitiveAxisView,
  SeriesAttachedParameter,
  Time,
} from 'lightweight-charts'
import { formatFibPrice, type FibDrawing, type FibRole } from '@/lib/fib-drawing'

const LINE_COLOR: Record<FibRole, string> = {
  low: '#64748b',
  retracement: '#64748b',
  roomLower: '#d97706',
  mid: '#64748b',
  supply: '#e11d48',
  high: '#e11d48',
  extension: '#e11d48',
}

const ROOM_FILL = 'rgba(245, 158, 11, 0.22)'
const SUPPLY_FILL = 'rgba(225, 29, 72, 0.30)'

interface FibFill {
  top: number
  height: number
  color: string
  label: string
  labelColor: string
  labelY: number
}

interface FibMark {
  y: number
  color: string
  label: string
  axisText: string
  dashed: boolean
  strong: boolean
}

export class FibLevelPrimitive implements ISeriesPrimitive<Time> {
  private series: ISeriesApi<'Candlestick', Time> | null = null
  private readonly paneView = new FibPaneView(this)
  private axisLabels: ISeriesPrimitiveAxisView[] = []

  constructor(
    readonly drawing: FibDrawing,
    readonly roomLabel: string,
    readonly supplyLabel: string,
  ) {}

  attached(param: SeriesAttachedParameter<Time, 'Candlestick'>) {
    this.series = param.series
  }

  detached() {
    this.series = null
  }

  updateAllViews() {
    this.paneView.update()
    this.axisLabels = this.paneView.marks.map((mark) => new FibAxisLabel(mark))
  }

  paneViews() {
    return [this.paneView]
  }

  priceAxisViews() {
    return this.axisLabels
  }

  autoscaleInfo(): AutoscaleInfo {
    return { priceRange: { minValue: this.drawing.swingLow, maxValue: this.drawing.swingHigh } }
  }

  priceY(ratio: number): number | null {
    const y = this.series?.priceToCoordinate(priceAt(this.drawing, ratio))
    return y == null ? null : y
  }
}

class FibPaneView implements IPrimitivePaneView {
  fills: FibFill[] = []
  marks: FibMark[] = []

  constructor(private readonly source: FibLevelPrimitive) {}

  zOrder() {
    return 'top' as const
  }

  update() {
    this.fills = collectFills(this.source)
    this.marks = collectMarks(this.source)
  }

  renderer(): IPrimitivePaneRenderer | null {
    return this.marks.length > 0 ? new FibRenderer(this.fills, this.marks) : null
  }
}

class FibRenderer implements IPrimitivePaneRenderer {
  constructor(
    private readonly fills: FibFill[],
    private readonly marks: FibMark[],
  ) {}

  draw(target: Parameters<IPrimitivePaneRenderer['draw']>[0]) {
    target.useMediaCoordinateSpace(({ context, mediaSize }) => {
      const width = mediaSize.width
      for (const fill of this.fills) paintFill(context, fill, width)
      for (const mark of this.marks) paintMark(context, mark, width)
    })
  }
}

class FibAxisLabel implements ISeriesPrimitiveAxisView {
  constructor(private readonly mark: FibMark) {}

  coordinate() {
    return -1_000_000
  }

  fixedCoordinate() {
    return this.mark.y
  }

  text() {
    return this.mark.axisText
  }

  textColor() {
    return '#ffffff'
  }

  backColor() {
    return this.mark.color
  }
}

function collectFills(source: FibLevelPrimitive): FibFill[] {
  const room = band(source, 0.382, 1, ROOM_FILL, source.roomLabel, '#d97706', 0.584)
  const supply = band(source, 0.786, 1, SUPPLY_FILL, source.supplyLabel, '#e11d48', 0.893)
  return [room, supply].filter((item): item is FibFill => item != null)
}

function collectMarks(source: FibLevelPrimitive): FibMark[] {
  const marks: FibMark[] = []
  for (const level of source.drawing.levels) {
    if (level.ratio > 1) continue
    const y = source.priceY(level.ratio)
    if (y == null) continue
    const strong = level.role === 'roomLower' || level.role === 'supply' || level.role === 'high'
    marks.push({
      y,
      color: LINE_COLOR[level.role],
      label: level.label,
      axisText: formatFibPrice(level.price),
      dashed: level.role === 'mid',
      strong,
    })
  }
  return marks
}

function band(
  source: FibLevelPrimitive,
  from: number,
  to: number,
  color: string,
  label: string,
  labelColor: string,
  labelRatio: number,
): FibFill | null {
  const top = source.priceY(to)
  const bottom = source.priceY(from)
  const labelY = source.priceY(labelRatio)
  if (top == null || bottom == null || labelY == null) return null
  const y = Math.min(top, bottom)
  return { top: y, height: Math.abs(bottom - top), color, label, labelColor, labelY }
}

function paintFill(context: CanvasRenderingContext2D, fill: FibFill, width: number) {
  context.fillStyle = fill.color
  context.fillRect(0, fill.top, width, fill.height)
  context.fillStyle = fill.labelColor
  context.font = '700 13px sans-serif'
  context.textBaseline = 'middle'
  context.fillText(fill.label, width * 0.42, fill.labelY)
}

function paintMark(context: CanvasRenderingContext2D, mark: FibMark, width: number) {
  context.strokeStyle = mark.color
  context.lineWidth = mark.strong ? 2 : 1
  context.setLineDash(mark.dashed ? [5, 4] : [])
  context.beginPath()
  context.moveTo(36, mark.y)
  context.lineTo(width, mark.y)
  context.stroke()
  context.setLineDash([])
  context.fillStyle = mark.color
  context.font = `${mark.strong ? 700 : 400} 11px sans-serif`
  context.textBaseline = 'middle'
  context.fillText(mark.label, 4, mark.y)
}

function priceAt(drawing: FibDrawing, ratio: number): number {
  return drawing.swingLow + (drawing.swingHigh - drawing.swingLow) * ratio
}
