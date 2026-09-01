/**
 * 吸筹/派发区底色。
 *
 * priceLines 只能画横线,填不出矩形,所以这里走 lightweight-charts 的 primitive
 * 直接在画布上刷一块半透明矩形。zOrder 走 'bottom',保证 K 线永远压在底色之上 ——
 * 底色是背景信息,不能盖住价格。
 */

import type {
  IChartApi,
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesApi,
  ISeriesPrimitive,
  PrimitivePaneViewZOrder,
  SeriesAttachedParameter,
  Time,
} from 'lightweight-charts'
import type { WyckoffZone } from '@wyckoff/shared'

interface ZoneBox {
  left: number
  right: number
  top: number
  bottom: number
  color: string
}

const ACCUMULATION_FILL = 'rgba(37, 99, 235, 0.10)'
const DISTRIBUTION_FILL = 'rgba(239, 68, 68, 0.10)'

class ZoneRenderer implements IPrimitivePaneRenderer {
  constructor(private readonly boxes: ZoneBox[]) {}

  draw(target: Parameters<IPrimitivePaneRenderer['draw']>[0]) {
    target.useMediaCoordinateSpace(({ context }) => {
      for (const box of this.boxes) {
        context.fillStyle = box.color
        context.fillRect(box.left, box.top, box.right - box.left, box.bottom - box.top)
      }
    })
  }
}

class ZonePaneView implements IPrimitivePaneView {
  private boxes: ZoneBox[] = []

  constructor(private readonly source: WyckoffZonePrimitive) {}

  zOrder(): PrimitivePaneViewZOrder {
    return 'bottom'
  }

  update() {
    this.boxes = this.source.computeBoxes()
  }

  renderer(): IPrimitivePaneRenderer | null {
    return this.boxes.length > 0 ? new ZoneRenderer(this.boxes) : null
  }
}

export class WyckoffZonePrimitive implements ISeriesPrimitive<Time> {
  private chart: IChartApi | null = null
  private series: ISeriesApi<'Candlestick', Time> | null = null
  private readonly paneView = new ZonePaneView(this)

  constructor(private zones: WyckoffZone[]) {}

  attached(param: SeriesAttachedParameter<Time>) {
    this.chart = param.chart as IChartApi
    this.series = param.series as ISeriesApi<'Candlestick', Time>
  }

  detached() {
    this.chart = null
    this.series = null
  }

  setZones(zones: WyckoffZone[]) {
    this.zones = zones
    this.paneView.update()
  }

  updateAllViews() {
    this.paneView.update()
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return [this.paneView]
  }

  computeBoxes(): ZoneBox[] {
    const chart = this.chart
    const series = this.series
    if (!chart || !series || this.zones.length === 0) return []
    const timeScale = chart.timeScale()
    const boxes: ZoneBox[] = []

    for (const zone of this.zones) {
      const left = timeScale.timeToCoordinate(zone.startDate as Time)
      const right = timeScale.timeToCoordinate(zone.endDate as Time)
      const top = series.priceToCoordinate(zone.priceHigh)
      const bottom = series.priceToCoordinate(zone.priceLow)
      // 区间滚出可视范围时坐标为 null,跳过 —— 不猜一个位置画上去。
      if (left == null || right == null || top == null || bottom == null) continue
      boxes.push({
        left: Math.min(left, right),
        right: Math.max(left, right),
        top: Math.min(top, bottom),
        bottom: Math.max(top, bottom),
        color: zone.structure === 'accumulation' ? ACCUMULATION_FILL : DISTRIBUTION_FILL,
      })
    }
    return boxes
  }
}
