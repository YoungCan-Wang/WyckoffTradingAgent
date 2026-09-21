import { describe, expect, it, vi } from 'vitest'
import { requestShadowLedger, shadowLedgerErrorMessage } from '../shadow-ledger-api'

function response(body: unknown, init?: ResponseInit): Response {
  return new Response(typeof body === 'string' ? body : JSON.stringify(body), init)
}

const showcase = {
  navCurve: [{ asOf: '2026-09-18', nav: 1.04 }],
  periodPnlAmount: 4000,
  periodPnlPct: 4,
  maxDrawdownPct: 1.8,
  winRatePct: null,
  winRateNote: '平仓样本不足，未计算粗胜率',
  openPositionCount: 2,
  sectorTags: ['传媒', '化工'],
}

describe('requestShadowLedger', () => {
  it('accepts a showcase DTO without authorization', async () => {
    const fetcher = vi.fn().mockResolvedValue(response({
      tier: 'showcase',
      accountId: 'USER_SHADOW:demo',
      asOf: '2026-09-18',
      disclaimer: '策略按威科夫漏斗跑纸面账；非投资建议',
      showcase,
    }))

    await expect(requestShadowLedger(undefined, undefined, fetcher)).resolves.toMatchObject({
      tier: 'showcase',
      showcase: { openPositionCount: 2 },
    })
    expect(fetcher).toHaveBeenCalledWith('http://127.0.0.1:8787/api/shadow-ledger', {
      headers: {},
    })
  })

  it('requires ledger details when the server claims full access', async () => {
    const fetcher = vi.fn().mockResolvedValue(response({
      tier: 'full',
      accountId: 'USER_SHADOW:demo',
      asOf: '2026-09-18',
      disclaimer: '策略按威科夫漏斗跑纸面账；非投资建议',
      showcase,
    }))

    await expect(requestShadowLedger('token', undefined, fetcher)).rejects.toThrow('不完整')
  })

  it('does not surface a raw Not Found to the page', async () => {
    expect(shadowLedgerErrorMessage({ error: 'Not Found' }, 404)).toContain('影子账户')
    const fetcher = vi.fn().mockResolvedValue(response({ error: 'Not Found' }, { status: 404 }))
    await expect(requestShadowLedger(undefined, undefined, fetcher)).rejects.toThrow('尚未就绪')
  })
})
