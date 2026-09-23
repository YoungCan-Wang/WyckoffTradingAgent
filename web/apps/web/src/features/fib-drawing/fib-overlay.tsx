import { FIB_PLOT_INSET, fibPlotY, type FibDrawing, type FibLevel } from '@/lib/fib-drawing'

const LINE_COLOR: Record<FibLevel['role'], string> = {
  low: '#94a3b8',
  retracement: '#94a3b8',
  roomLower: '#f59e0b',
  mid: '#94a3b8',
  supply: '#fb7185',
  high: '#fb7185',
  extension: '#fb7185',
}

export function FibOverlay({ drawing }: { drawing: FibDrawing }) {
  const band = supplyBand(drawing)
  return (
    <svg className="pointer-events-none absolute inset-0 z-10 h-full w-full" aria-hidden="true">
      {band && (
        <rect
          x={`${FIB_PLOT_INSET.left * 100}%`}
          y={`${band.top * 100}%`}
          width={`${(1 - FIB_PLOT_INSET.left - FIB_PLOT_INSET.right) * 100}%`}
          height={`${band.height * 100}%`}
          fill="#fb7185"
          opacity={0.16}
        />
      )}
      {drawing.levels.filter((level) => level.ratio <= 1).map((level) => (
        <FibLine key={level.label} level={level} drawing={drawing} />
      ))}
    </svg>
  )
}

function FibLine({ level, drawing }: { level: FibLevel; drawing: FibDrawing }) {
  const y = fibPlotY(level.price, drawing.swingLow, drawing.swingHigh)
  if (y == null) return null
  const color = LINE_COLOR[level.role]
  const yPct = `${y * 100}%`
  const x1 = `${(FIB_PLOT_INSET.left + 0.1) * 100}%`
  const x2 = `${(1 - FIB_PLOT_INSET.right) * 100}%`
  return (
    <g>
      <line
        x1={x1}
        x2={x2}
        y1={yPct}
        y2={yPct}
        stroke={color}
        strokeWidth={level.role === 'roomLower' || level.role === 'high' ? 2 : 1.25}
        strokeDasharray={level.role === 'mid' ? '5 4' : undefined}
      />
      <text x={`${FIB_PLOT_INSET.left * 100}%`} y={yPct} fill={color} fontSize="11" dominantBaseline="middle">
        {level.label}
      </text>
    </g>
  )
}

function supplyBand(drawing: FibDrawing): { top: number; height: number } | null {
  const upper = levelY(drawing, 1)
  const lower = levelY(drawing, 0.786)
  if (upper == null || lower == null) return null
  const top = Math.min(upper, lower)
  return { top, height: Math.abs(lower - upper) }
}

function levelY(drawing: FibDrawing, ratio: number): number | null {
  const level = drawing.levels.find((item) => item.ratio === ratio)
  if (!level) return null
  return fibPlotY(level.price, drawing.swingLow, drawing.swingHigh)
}
