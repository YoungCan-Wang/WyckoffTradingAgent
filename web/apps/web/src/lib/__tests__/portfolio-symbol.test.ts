import { describe, expect, it } from 'vitest'
import { isSupportedPortfolioCode, normalizePortfolioCode } from '@wyckoff/shared'

describe('normalizePortfolioCode', () => {
  it('normalizes CN/HK/US holdings codes', () => {
    expect(normalizePortfolioCode('601881')).toBe('601881')
    expect(normalizePortfolioCode('6881.HK')).toBe('06881.HK')
    expect(normalizePortfolioCode('AAPL')).toBe('AAPL.US')
    expect(isSupportedPortfolioCode('00700.HK')).toBe(true)
    expect(isSupportedPortfolioCode('6881')).toBe(false)
  })
})
