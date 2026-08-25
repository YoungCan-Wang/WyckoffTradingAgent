import { describe, expect, it } from 'vitest'
import { pairingCodeFromHash } from './use-remote'

describe('pairingCodeFromHash', () => {
  it('读出电脑生成的配对码', () => {
    expect(pairingCodeFromHash('#code=abc1234567')).toBe('abc1234567')
  })

  it('容忍前面还有其他参数', () => {
    expect(pairingCodeFromHash('#foo=1&code=deadbeef00')).toBe('deadbeef00')
  })

  it('没有码时返回空串而不是 undefined', () => {
    // 调用方用 `if (!code && !paired)` 判断，undefined 会让类型层面变得含糊。
    expect(pairingCodeFromHash('')).toBe('')
    expect(pairingCodeFromHash('#other=x')).toBe('')
  })

  it('不把相似的键名当成 code', () => {
    // `mycode=` 不是 `code=`。前缀不锚定会让它误匹配。
    expect(pairingCodeFromHash('#mycode=abc1234567')).toBe('')
  })

  it('忽略非法字符', () => {
    // 配对码是 hex 片段；带别的字符说明这个链接被改过。
    expect(pairingCodeFromHash('#code=abc-123')).toBe('abc')
  })
})
