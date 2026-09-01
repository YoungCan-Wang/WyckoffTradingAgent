/**
 * 复权口径:结构分析用前复权,报单价用实时价,两者必须对得上。
 *
 * K 线取的是 `adjust: 'forward'`(前复权):历史价按今天的价格尺度还原,所以
 * 最新一根的收盘就等于当天的真实收盘,历史上的 Spring / SOS 价位也都落在
 * 今天这把尺子上 —— 可以直接拿来当挂单参考。
 *
 * 会出事的是两个尺子悄悄错开:除权后数据源还没重算、或者 K 线停在几天前。
 * 这时候「支撑位 12.30」和盘口的 12.30 已经不是同一个价。算不出谁对,
 * 但能测出差多少 —— 差得离谱就别报单价,先说清楚。
 */

/** 收盘价与实时价的相对偏离超过这个比例就不当作同一把尺子。 */
const BASIS_DIVERGENCE_THRESHOLD = 0.02

export type PriceBasisStatus = 'aligned' | 'diverged' | 'unknown'

export interface PriceBasisCheck {
  status: PriceBasisStatus
  /** 前复权最新收盘。 */
  adjustedClose: number | null
  /** 实时最新价(不复权)。 */
  livePrice: number | null
  /** 相对偏离,livePrice 为基准。 */
  divergencePct: number | null
}

export function checkPriceBasis(adjustedClose: number | null, livePrice: number | null): PriceBasisCheck {
  const usable = typeof adjustedClose === 'number' && Number.isFinite(adjustedClose) && adjustedClose > 0
    && typeof livePrice === 'number' && Number.isFinite(livePrice) && livePrice > 0
  if (!usable) {
    return { status: 'unknown', adjustedClose, livePrice, divergencePct: null }
  }
  const divergence = Math.abs(adjustedClose - livePrice) / livePrice
  return {
    status: divergence <= BASIS_DIVERGENCE_THRESHOLD ? 'aligned' : 'diverged',
    adjustedClose,
    livePrice,
    divergencePct: divergence,
  }
}

/** 给模型看的口径说明。报单价只在两把尺子对得上时才允许直接引用结构价位。 */
export function formatPriceBasisNote(check: PriceBasisCheck): string {
  const head = '## 复权口径'
  const base = 'K 线为前复权(历史价已折算到当前价格尺度),结构价位与实时价可直接比较;实时最新价为不复权成交价。'
  if (check.status === 'aligned') {
    return [head, base, '本轮两者一致,T+1 委托价可以直接引用结构价位。'].join('\n')
  }
  if (check.status === 'diverged') {
    const pct = ((check.divergencePct ?? 0) * 100).toFixed(2)
    return [
      head,
      base,
      `注意:前复权最新收盘 ${check.adjustedClose} 与实时最新价 ${check.livePrice} 相差 ${pct}%,两把尺子没对上(可能刚除权、数据源未重算,或 K 线不是最新交易日)。`,
      '这种情况下不要把结构价位直接当作委托价报出去,先说明口径差异,报单价以实时价为准。',
    ].join('\n')
  }
  return [head, base, '本轮缺少实时价,无法核对两者是否一致;若要给出 T+1 委托价,需明确说明其来自前复权收盘而非实时成交价。'].join('\n')
}
