import { useEffect, useRef, useState } from 'react'

const t = (key: string) => window.WyckoffI18n.t(key)

/**
 * 登录页。**取代整个工作台**，不是弹窗。
 *
 * 为什么不是弹窗：登录是进入应用的前置条件，而弹窗暗示「可以关掉继续用」。
 * 用户可能把模型和行情数据源都配在云端，没登录时那些配置拉不下来 ——
 * 界面会显示「未配置模型」，看起来像应用坏了。
 *
 * 原来的账号菜单里只有「退出登录」：未登录时点开等于只剩设置，是条死路。
 */

interface Props {
  /** 调 IPC。返回 null 表示失败（错误已由上层记录）。 */
  collect: (method: string, params?: Record<string, unknown>) => Promise<Record<string, unknown> | null>
  /** 登录成功，带上云端同步结果供上层提示。 */
  onSignedIn: (info: { email: string; synced?: Record<string, unknown> }) => void
  /** 后端还没就绪时禁用提交 —— 否则点了没反应，看起来像卡死。 */
  backendReady: boolean
}

export function LoginScreen ({ collect, onSignedIn, backendReady }: Props) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const emailRef = useRef<HTMLInputElement>(null)

  // 进来就聚焦邮箱：这一屏只有一件事可做。
  useEffect(() => {
    emailRef.current?.focus()
  }, [])

  const submit = async (event?: React.FormEvent) => {
    event?.preventDefault()
    if (busy || !backendReady) return
    const mail = email.trim()
    if (!mail || !password) {
      setError(t('login.needBoth'))
      return
    }
    setBusy(true)
    setError('')
    try {
      // 密码只往下走一次，不进任何状态之外的地方。
      const res = await collect('auth_login', { email: mail, password })
      if (!res || !res.signed_in) {
        setError(t('login.failed'))
        return
      }
      // 成功后立刻清掉密码，不让它留在组件状态里等 GC。
      setPassword('')
      onSignedIn({
        email: String(res.email || mail),
        synced: (res.synced as Record<string, unknown>) || undefined
      })
    } catch (err) {
      // 后端把「密码不对」和「网络不通」分成了两个 code，那两件事的下一步动作
      // 完全不同 —— 统一报「登录失败」会让用户反复试密码而其实是网络问题。
      const detail = err instanceof Error ? err.message : String(err)
      setError(/bad_credentials|不正确/.test(detail) ? t('login.badCredentials') : t('login.failed'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-wrap">
      <form className="login-card" onSubmit={submit}>
        <h1 className="login-t">{t('login.title')}</h1>
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
          onChange={(e) => setEmail(e.target.value)}
        />

        <label className="login-l" htmlFor="login-pw">{t('login.password')}</label>
        <input
          id="login-pw"
          className="login-i"
          type="password"
          autoComplete="current-password"
          value={password}
          disabled={busy}
          onChange={(e) => setPassword(e.target.value)}
        />

        {/* role=alert：错误要被读屏立刻念出来，而不是等用户自己发现 */}
        {error ? <p className="login-err" role="alert">{error}</p> : null}

        <button className="login-go" type="submit" disabled={busy || !backendReady}>
          {busy ? t('login.busy') : t('login.submit')}
        </button>

        {!backendReady ? <p className="login-hint">{t('login.waitingBackend')}</p> : null}
        <p className="login-hint">{t('login.cliHint')}</p>
      </form>
    </div>
  )
}
