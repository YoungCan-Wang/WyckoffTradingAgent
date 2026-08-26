/**
 * 登录弹窗。从侧栏左下角的账号行打开。
 *
 * 为什么是弹窗而不是全屏页：应用**不登录也能用**（持仓存本地 SQLite、模型可以
 * 本地配）。登录只是额外接上云端 —— 把它做成进门必答题，等于把一个可选能力
 * 变成强制关卡。我上一版做成全屏取代工作台是过头了。
 *
 * 焦点陷阱与 inert 的处理照抄 SettingsModal：那份已经在 e2e 里验过，
 * 另写一套只会多一个漏 Tab 的地方。
 */
import { useEffect, useRef, useState } from 'react'

const t = (key: string) => window.WyckoffI18n.t(key)

interface Props {
  open: boolean
  onClose: () => void
  /** 调 IPC。返回 null 表示失败（错误已由上层记录）。 */
  collect: (method: string, params?: Record<string, unknown>) => Promise<Record<string, unknown> | null>
  /** 登录成功，带上云端同步结果供上层提示。 */
  onSignedIn: (info: { email: string; synced?: Record<string, unknown> }) => void
  /** 后端还没就绪时不能提交 —— 但字段校验仍然要给反馈。 */
  backendReady: boolean
  /**
   * 上次登录用的邮箱，用于预填。
   *
   * **只预填邮箱，永不预填密码。** 邮箱是「你是谁」，记住它是便利；密码是凭据，
   * 预填它等于让「退出登录」名不副实。
   */
  initialEmail?: string
}

export function LoginModal ({ open, onClose, collect, onSignedIn, backendReady, initialEmail = '' }: Props) {
  const [email, setEmail] = useState(initialEmail)
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  // 用户动过输入框之后就不再接受外部同步 —— 不能冲掉他正在打的字。
  const [touched, setTouched] = useState(false)
  const [error, setError] = useState('')
  const card = useRef<HTMLDivElement>(null)
  const emailRef = useRef<HTMLInputElement>(null)
  const pwRef = useRef<HTMLInputElement>(null)

  // `initialEmail` 是晚到的：它来自 `account`，而那要等 Python 后端起来
  // （打包后要几秒）。只用 useState 初值的话预填永远不生效。
  useEffect(() => {
    if (initialEmail && !touched) setEmail(initialEmail)
  }, [initialEmail, touched])

  // 打开时禁用背景交互并把焦点移进来。邮箱已预填时直接给密码框 ——
  // 焦点落在一个已填好的字段上，用户得先按一次 Tab。
  useEffect(() => {
    const background = [...document.querySelectorAll<HTMLElement>('.side, .thread, .pane-resizer, .pane')]
    for (const node of background) node.inert = open
    if (open) {
      requestAnimationFrame(() => {
        if (initialEmail) pwRef.current?.focus()
        else emailRef.current?.focus()
      })
    }
    return () => { for (const node of background) node.inert = false }
  }, [open, initialEmail])

  // 关闭时清掉密码与错误：下次打开不该看到上一次的残留。
  useEffect(() => {
    if (open) return
    setPassword('')
    setError('')
    setBusy(false)
  }, [open])

  // Esc 关闭 + Tab 在弹窗内环形回绕（理由见 SettingsModal 里的说明）。
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { onClose(); return }
      if (e.key !== 'Tab') return
      const dlg = card.current
      if (!dlg) return
      const focusable = [...dlg.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])'
      )].filter((n) => !n.hidden && n.getClientRects().length > 0)
      if (!focusable.length) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus() }
      else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus() }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onClose])

  const submit = async (event?: React.FormEvent) => {
    event?.preventDefault()
    if (busy) return
    // 字段校验放在 backendReady 之前：「请填写邮箱和密码」不需要后端。
    // 顺序相反时，后端启动那几秒提交空表单完全没有反馈。
    const mail = email.trim()
    if (!mail || !password) {
      setError(t('login.needBoth'))
      return
    }
    if (!backendReady) {
      setError(t('login.waitingBackend'))
      return
    }
    setBusy(true)
    setError('')
    try {
      const res = await collect('auth_login', { email: mail, password })
      if (!res || !res.signed_in) {
        setError(t('login.failed'))
        return
      }
      setPassword('')
      onSignedIn({
        email: String(res.email || mail),
        synced: (res.synced as Record<string, unknown>) || undefined
      })
      onClose()
    } catch (err) {
      // 后端把「密码不对」和「网络不通」分成两个 code：统一报「登录失败」
      // 会让用户反复试密码而其实是网络问题。
      const detail = err instanceof Error ? err.message : String(err)
      setError(/bad_credentials|不正确/.test(detail) ? t('login.badCredentials') : t('login.failed'))
    } finally {
      setBusy(false)
    }
  }

  if (!open) return null

  return (
    <div className="ov" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div className="dlg login-dlg" role="dialog" aria-modal="true" aria-label={t('login.title')} ref={card}>
        <form className="login-form" onSubmit={submit}>
          <h2 className="login-t">{t('login.title')}</h2>
          <p className="login-sub">{t('login.sub')}</p>

          <label className="login-l" htmlFor="login-email">{t('login.email')}</label>
          <input
            id="login-email"
            ref={emailRef}
            className="login-i"
            type="email"
            autoComplete="username"
            spellCheck={false}
            value={email}
            disabled={busy}
            onChange={(e) => { setTouched(true); setEmail(e.target.value) }}
          />

          <label className="login-l" htmlFor="login-pw">{t('login.password')}</label>
          <input
            id="login-pw"
            ref={pwRef}
            className="login-i"
            type="password"
            autoComplete="current-password"
            value={password}
            disabled={busy}
            onChange={(e) => setPassword(e.target.value)}
          />

          {/* role=alert：错误要被读屏立刻念出来 */}
          {error ? <p className="login-err" role="alert">{error}</p> : null}

          <div className="login-acts">
            <button className="login-cancel" type="button" onClick={onClose} disabled={busy}>
              {t('login.cancel')}
            </button>
            <button className="login-go" type="submit" disabled={busy}>
              {/* 转圈而不只是换文字：登录要等网络往返（实测好几秒），
                  静止的「登录中…」看着像卡死了。 */}
              {busy ? <><span className="spin" aria-hidden="true" />{t('login.busy')}</> : t('login.submit')}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
