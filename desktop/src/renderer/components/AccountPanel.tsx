/**
 * 「账号」页。
 *
 * 自己读一次 account，避免设置页复用外壳状态时拿到过期账号。退出动作交回
 * App，统一刷新侧栏、缓存和弹窗状态。
 */
import { useEffect, useState } from 'react'
import { collect } from '../lib/ipc'

const t = (key: string) => window.WyckoffI18n.t(key)

interface Account {
  signed_in: boolean
  email: string
}

export function AccountPanel ({ onSignOut }: { onSignOut: () => void }) {
  const [account, setAccount] = useState<Account | null>(null)
  const [loading, setLoading] = useState(true)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let alive = true
    collect('account')
      .then((res) => {
        if (!alive) return
        setAccount(res ? (res as unknown as Account) : null)
        setFailed(res === null)
        setLoading(false)
      })
      .catch(() => {
        if (!alive) return
        setFailed(true)
        setLoading(false)
      })
    return () => { alive = false }
  }, [])

  if (loading) return <p className="empty">{t('common.loading')}</p>
  if (failed || !account) return <p className="empty">{t('signin.readFailed')}</p>

  const signedIn = account.signed_in

  return (
    <>
      <div className="srow">
        <span className="slab">{t('signin.current')}</span>
        <span className={signedIn ? 'ok' : 'miss'}>
          {account.email || t('account.signedOut')}
        </span>
      </div>
      {signedIn ? (
        <>
          <p className="dlg-sub">{t('signin.signoutNote')}</p>
          <button type="button" className="wel-c" onClick={onSignOut}>
            {t('signin.signout')}
          </button>
        </>
      ) : (
        <p className="dlg-sub">{t('signin.reloginHint')}</p>
      )}
    </>
  )
}
