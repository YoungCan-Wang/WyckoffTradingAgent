/**
 * 设置 · 手机遥控。
 *
 * 开启后电脑连上云端信箱，手机扫码配对，之后手机上的操作和在电脑上一模一样
 * （对话、改持仓、设止损、审批 —— 权限完全一致，这是明确的产品决定）。
 *
 * ## 关于睡眠
 *
 * 实测过三种状态：锁屏和息屏**都不影响**（进程照常、网络零断连）；只有**系统
 * 睡眠**会断。所以开启期间挂 powerSaveBlocker('prevent-app-suspension') ——
 * 挡住空闲睡眠，但屏幕照常关（用 prevent-display-sleep 会白白耗电还烧屏）。
 *
 * 合盖挡不住，那是硬件级的；但那时用户就在电脑边，不需要遥控。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { collect } from '../lib/ipc'
import { SecHead } from './Rows'

const t = (key: string, params?: Record<string, string>) => window.WyckoffI18n.t(key, params)

interface Device {
  conn_id: string
  role: string
  label: string
  since: number
}

export function RemotePanel () {
  const [enabled, setEnabled] = useState(false)
  const [connected, setConnected] = useState(false)
  const [signedIn, setSignedIn] = useState(true)
  const [devices, setDevices] = useState<Device[]>([])
  const [pairUrl, setPairUrl] = useState('')
  const [expiresAt, setExpiresAt] = useState(0)
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<{ text: string; error?: boolean } | null>(null)
  const canvas = useRef<HTMLCanvasElement | null>(null)

  const flash = useCallback((text: string, error = false) => {
    setNote({ text, error })
    setTimeout(() => setNote(null), 3200)
  }, [])

  const refresh = useCallback(async () => {
    const status = await collect('remote_status').catch(() => null)
    if (status) {
      setEnabled(Boolean(status.running))
      setConnected(Boolean(status.connected))
      setSignedIn(Boolean(status.signed_in))
    }
    // 设备列表要连上云端才有意义；没开启时不打扰后端。
    if (status?.running) {
      const list = await collect('remote_devices').catch(() => null)
      setDevices(((list?.devices as Device[]) || []).filter((d) => d.role === 'remote'))
    } else {
      setDevices([])
    }
  }, [])

  useEffect(() => { void refresh() }, [refresh])

  // 开启期间轮询在线状态。连接会因为网络抖动断开重连，界面要跟着变 ——
  // 显示「已连接」而实际断了比显示「未连接」更糟。
  useEffect(() => {
    if (!enabled) return
    const timer = setInterval(() => void refresh(), 5000)
    return () => clearInterval(timer)
  }, [enabled, refresh])

  // 配对码有 TTL，过期了要把二维码收掉 —— 留着一个已经失效的码，用户扫了没反应
  // 会以为是功能坏了。
  useEffect(() => {
    if (!expiresAt) return
    const remain = expiresAt - Date.now()
    if (remain <= 0) { setPairUrl(''); return }
    const timer = setTimeout(() => { setPairUrl(''); setExpiresAt(0) }, remain)
    return () => clearTimeout(timer)
  }, [expiresAt])

  // 画二维码。动态 import：qrcode 有 135KB，只有真正要配对时才加载。
  useEffect(() => {
    if (!pairUrl || !canvas.current) return
    let alive = true
    void import('qrcode').then((qr) => {
      if (!alive || !canvas.current) return
      qr.toCanvas(canvas.current, pairUrl, { width: 190, margin: 1 }).catch(() => {
        flash(t('remote.qrFailed'), true)
      })
    })
    return () => { alive = false }
  }, [pairUrl, flash])

  const toggle = async () => {
    setBusy(true)
    try {
      if (enabled) {
        const res = await collect('remote_disable').catch(() => null)
        if (!res) { flash(t('remote.disableFailed'), true); return }
        await window.wyckoff.keepAwake?.(false)
        setEnabled(false)
        setPairUrl('')
        setDevices([])
        flash(t('remote.disabled'))
        return
      }
      const res = await collect('remote_enable').catch(() => null)
      if (!res) { flash(t('remote.enableFailed'), true); return }
      // 先阻止睡眠再宣布开启：反过来的话有个窗口期用户以为能用、电脑却会睡过去。
      await window.wyckoff.keepAwake?.(true)
      setEnabled(true)
      flash(t('remote.enabled'))
      void refresh()
    } finally {
      setBusy(false)
    }
  }

  const pair = async () => {
    setBusy(true)
    try {
      const res = await collect('remote_pair').catch(() => null)
      if (!res?.url) { flash(t('remote.pairFailed'), true); return }
      setPairUrl(String(res.url))
      setExpiresAt(Date.now() + Number(res.expires_in_ms || 0))
    } finally {
      setBusy(false)
    }
  }

  const revoke = async (connId: string, label: string) => {
    if (!window.confirm(t('remote.revokeConfirm', { name: label }))) return
    await collect('remote_revoke', { conn_id: connId }).catch(() => null)
    void refresh()
  }

  const revokeAll = async () => {
    if (!window.confirm(t('remote.revokeAllConfirm'))) return
    await collect('remote_revoke', { conn_id: '*' }).catch(() => null)
    setPairUrl('')
    void refresh()
  }

  if (!signedIn) {
    return (
      <div data-testid="remote-panel">
        <SecHead k="remote.title" />
        <p className="dlg-sub">{t('remote.needLogin')}</p>
      </div>
    )
  }

  return (
    <div data-testid="remote-panel">
      <SecHead k="remote.title" />
      <p className="dlg-sub">{t('remote.intro')}</p>

      <div className="rc-row">
        <div className="rc-state">
          <span className={connected ? 'rc-dot on' : 'rc-dot'} aria-hidden="true" />
          <span>
            {!enabled ? t('remote.stateOff') : connected ? t('remote.stateOn') : t('remote.stateConnecting')}
          </span>
        </div>
        <button type="button" className="mbtn" disabled={busy} onClick={() => void toggle()} data-testid="remote-toggle">
          {enabled ? t('remote.turnOff') : t('remote.turnOn')}
        </button>
      </div>

      {enabled ? (
        <>
          {/* 睡眠边界要说清楚 —— 用户以为「锁屏就断了」，实际不是。 */}
          <p className="rc-hint">{t('remote.sleepNote')}</p>

          <div className="rc-pair">
            {pairUrl ? (
              <div className="rc-qr">
                <canvas ref={canvas} aria-label={t('remote.qrLabel')} />
                <p className="rc-qr-hint">{t('remote.qrHint')}</p>
                <button type="button" className="mbtn" onClick={() => setPairUrl('')}>
                  {t('remote.hideQr')}
                </button>
              </div>
            ) : (
              <button type="button" className="mbtn" disabled={busy} onClick={() => void pair()} data-testid="remote-pair">
                {t('remote.showQr')}
              </button>
            )}
          </div>

          <div className="rc-devices">
            <div className="rc-devices-h">
              <span>{t('remote.devices')}</span>
              {devices.length ? (
                <button type="button" className="mbtn danger" onClick={() => void revokeAll()}>
                  {t('remote.revokeAll')}
                </button>
              ) : null}
            </div>
            {devices.length === 0 ? (
              <p className="rc-empty">{t('remote.noDevices')}</p>
            ) : (
              devices.map((d) => (
                <div className="rc-device" key={d.conn_id} data-device={d.conn_id}>
                  <span className="rc-device-n">{d.label}</span>
                  <button type="button" className="mbtn danger" onClick={() => void revoke(d.conn_id, d.label)}>
                    {t('remote.revoke')}
                  </button>
                </div>
              ))
            )}
          </div>
        </>
      ) : null}

      {note ? <span className={note.error ? 'snote err' : 'snote'}>{note.text}</span> : null}
    </div>
  )
}
