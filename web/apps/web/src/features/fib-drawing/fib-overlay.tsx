import { FIB_PLOT_INSET, fibPlotY, type FibDrawing, type FibLevel } from '@/lib/fib-drawing'

const LINE_COLOR: Record<FibLevel['role'], string> = {
  low: '#94a3b8',
  retracement: '#94a3b8',
  roomLower: '#d97706',
  mid: '#94a3b8',
  supply: '#e11d48',
  high: '#e11d48',
  extension: '#e11d48',
}

export function FibOverlay({
  drawing,
  roomLabel,
  supplyLabel,
}: {
  drawing: FibDrawing
  roomLabel: string
  supplyLabel: string
}) {
  return (
    <svg className="pointer-events-none absolute inset-0 z-10 h-full w-full" aria-hidden="true">
      <FibBand drawing={drawing} from={0.382} to={1} fill="#f59e0b" opacity={0.28} label={roomLabel} labelRatio={0.58} />
      <FibBand drawing={drawing} from={0.786} to={1} fill="#e11d48" opacity={0.38} label={supplyLabel} />
      {drawing.levels.filter((level) => level.ratio <= 1).map((level) => (
        <FibLine key={level.label} level={level} drawing={drawing} />
      ))}
    </svg>
  )
}

function FibBand({
  drawing,
  from,
  to,
  fill,
  opacity,
  label,
  labelRatio,
}: {
  drawing: FibDrawing
  from: number
  to: number
  fill: string
  opacity: number
  label: string
  labelRatio?: number
}) {
  const top = levelY(drawing, to)
  const bottom = levelY(drawing, from)
  if (top == null || bottom == null) return null
  const y = Math.min(top, bottom)
  const height = Math.abs(bottom - top)
  const textY = labelRatio == null ? y + height / 2 : levelY(drawing, labelRatio)
  return (
    <g>
      <rect
        x={`${FIB_PLOT_INSET.left * 100}%`}
        y={`${y * 100}%`}
        width={`${(1 - FIB_PLOT_INSET.left - FIB_PLOT_INSET.right) * 100}%`}
        height={`${height * 100}%`}
        fill={fill}
        opacity={opacity}
      />
      {textY != null && (
        <text x="58%" y={`${textY * 100}%`} fill={fill} fontSize="13" fontWeight="700" dominantBaseline="middle">
          {label}
        </text>
      )}
    </g>
  )
}

function FibLine({ level, drawing }: { level: FibLevel; drawing: FibDrawing }) {
  const y = fibPlotY(level.price, drawing.swingLow, drawing.swingHigh)
  if (y == null) return null
  const color = LINE_COLOR[level.role]
  const yPct = `${y * 100}%`
  const x1 = `${(FIB_PLOT_INSET.left + 0.1) * 100}%`
  const x2 = `${(1 - FIB_PLOT_INSET.right) * 100}%`
  const strong = level.role === 'roomLower' || level.role === 'high' || level.role === 'supply'
  return (
    <g>
      <line x1={x1} x2={x2} y1={yPct} y2={yPct} stroke={color} strokeWidth={strong ? 2.25 : 1.25} strokeDasharray={level.role === 'mid' ? '5 4' : undefined} />
      <text x={`${FIB_PLOT_INSET.left * 100}%`} y={yPct} fill={color} fontSize="11" fontWeight={strong ? 700 : 400} dominantBaseline="middle">
        {level.label}
      </text>
    </g>
  )
}

function levelY(drawing: FibDrawing, ratio: number): number | null {
  const level = drawing.levels.find((item) => item.ratio === ratio)
  const price = level?.price ?? drawing.swingLow + (drawing.swingHigh - drawing.swingLow) * ratio
  return fibPlotY(price, drawing.swingLow, drawing.swingHigh)
}
