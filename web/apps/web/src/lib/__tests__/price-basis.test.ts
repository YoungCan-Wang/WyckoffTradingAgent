import { describe, expect, it } from 'vitest'
import { checkPriceBasis, formatPriceBasisNote } from '@wyckoff/shared'

describe('checkPriceBasis', () => {
  it('前复权收盘与实时价一致时算对齐', () => {
    expect(checkPriceBasis(12.3, 12.32)).toMatchObject({ status: 'aligned' })
  })

  it('差出阈值就判为错开,不当作同一把尺子', () => {
    const check = checkPriceBasis(12.3, 20.5)
    expect(check.status).toBe('diverged')
    expect(check.divergencePct).toBeCloseTo(0.4, 2)
  })

  it('缺任一边就是 unknown,不许拿单边数当依据', () => {
    expect(checkPriceBasis(null, 12.3).status).toBe('unknown')
    expect(checkPriceBasis(12.3, null).status).toBe('unknown')
  })

  it('零和负数不是有效价格', () => {
    expect(checkPriceBasis(0, 12.3).status).toBe('unknown')
    expect(checkPriceBasis(12.3, -1).status).toBe('unknown')
  })

  it('NaN 不能被当成数字放过去', () => {
    expect(checkPriceBasis(Number.NaN, 12.3).status).toBe('unknown')
  })
})

describe('formatPriceBasisNote', () => {
  it('对齐时允许直接引用结构价位报单', () => {
    const text = formatPriceBasisNote(checkPriceBasis(12.3, 12.3))
    expect(text).toContain('可以直接引用结构价位')
  })

  it('错开时必须先说明,且报单价以实时价为准', () => {
    const text = formatPriceBasisNote(checkPriceBasis(12.3, 20.5))
    expect(text).toContain('两把尺子没对上')
    expect(text).toContain('不要把结构价位直接当作委托价')
    expect(text).toContain('报单价以实时价为准')
  })

  it('缺实时价时要求标明价来自前复权收盘', () => {
    const text = formatPriceBasisNote(checkPriceBasis(12.3, null))
    expect(text).toContain('无法核对')
    expect(text).toContain('前复权收盘而非实时成交价')
  })
})
