/**
 * 「定时任务」栏：调度进程状态，以及残留 launchd 服务的清理入口。
 *
 * 单独一次 daemon_status 调用，与 settings_get 无关，所以自己管刷新。
 */
import { useCallback, useEffect, useState } from 'react'
import { collect } from '../lib/ipc'
import { SecHead } from './Rows'

const t = (key: string) => window.WyckoffI18n.t(key)

interface Status {
  running: boolean
  installed: boolean
}

export function DaemonSection ({ onMessage }: { onMessage: (text: string, isError?: boolean) => void }) {
  const [status, setStatus] = useState<Status | null>(null)
  const [loading, setLoading] = useState(true)
  const [failed, setFailed] = useState(false)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    const res = await collect('daemon_status').catch(() => null)
    setStatus(res ? (res as unknown as Status) : null)
    setFailed(res === null)
    setLoading(false)
  }, [])

  useEffect(() => { void load() }, [load])

  const uninstall = async () => {
    if (!window.confirm(t('daemon.removeConfirm'))) return
    setBusy(true)
    const res = await collect('daemon_uninstall').catch(() => null)
    setBusy(false)
    if (!res) onMessage(t('daemon.removeFailed'), true)
    await load()
  }

  return (
    <>
      <SecHead k="settings.daemon" />
      <p className="dlg-sub">{t('schedules.daemonNote')}</p>

      {loading ? <p className="empty">{t('schedules.loading')}</p> : null}
      {/* 读不到状态就说读不到，不要显示成「未运行」—— 那是另一回事 */}
      {!loading && failed ? <p className="empty">{t('schedules.statusReadFailed')}</p> : null}

      {!loading && status ? (
        <>
          <div className="srow">
            <div className="sleft">
              <span className="slab">{t('daemon.schedProcess')}</span>
              <span className="shint">{status.running ? t('daemon.running') : t('daemon.notRunning')}</span>
            </div>
            <span className={status.running ? 'ok' : 'miss'}>
              {status.running ? t('daemon.stateRunning') : t('daemon.stateStopped')}
            </span>
          </div>

          {/* 只在真的存在时才提：残留的 launchd 服务会在应用关闭时也跑任务，
              与「随应用启停」的设计相矛盾，属于需要用户知道并清理的状态。 */}
          {status.installed ? (
            <>
              <div className="srow">
                <div className="sleft">
                  <span className="slab">{t('daemon.autostart')}</span>
                  <span className="shint">{t('daemon.autostartDetected')}</span>
                </div>
              </div>
              <button type="button" className="wel-c" disabled={busy} onClick={() => void uninstall()}>
                {busy ? t('daemon.processing') : t('daemon.removeAutostart')}
              </button>
            </>
          ) : null}

          <button type="button" className="wel-c" onClick={() => void load()}>
            {t('daemon.refresh')}
          </button>
        </>
      ) : null}
    </>
  )
}
