import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  clearRemotePairingState,
  pairingCodeFromHash,
  readStoredDeviceToken,
  storeDeviceGrant,
} from './use-remote'

describe('pairingCodeFromHash', () => {
  it('读出电脑生成的配对码', () => {
    expect(pairingCodeFromHash('#code=abc1234567')).toBe('abc1234567')
  })

  it('容忍前面还有其他参数', () => {
    expect(pairingCodeFromHash('#foo=1&code=deadbeef00')).toBe('deadbeef00')
  })

  it('没有码时返回空串而不是 undefined', () => {
    // 调用方用 `if (!code && !device)` 判断，undefined 会让类型层面变得含糊。
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

describe('device grant storage', () => {
  beforeEach(() => {
    const store = new Map<string, string>()
    vi.stubGlobal('sessionStorage', {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => { store.set(key, value) },
      removeItem: (key: string) => { store.delete(key) },
    })
  })

  it('保存设备凭证供断线重连', () => {
    storeDeviceGrant('abcd'.repeat(8))
    expect(readStoredDeviceToken()).toBe('abcd'.repeat(8))
  })

  it('清空配对态时一并丢掉设备凭证', () => {
    storeDeviceGrant('abcd'.repeat(8))
    clearRemotePairingState()
    expect(readStoredDeviceToken()).toBe('')
  })
})
