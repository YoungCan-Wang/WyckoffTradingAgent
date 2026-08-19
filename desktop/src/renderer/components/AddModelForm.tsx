/** 新增模型表单。默认折叠：加模型是偶发操作，不是主任务。 */
import { useState } from 'react'
import { collect } from '../lib/ipc'

const t = (key: string, params?: Record<string, string | number>) => window.WyckoffI18n.t(key, params)

const PROVIDERS = ['openai', 'gemini', 'claude']

interface Props {
  onAdded: () => Promise<void> | void
  /** 结果用系统消息告知（保存成功但连不通也要说清楚）。 */
  onMessage: (text: string, isError?: boolean) => void
}

const EMPTY = { id: '', model: '', api_key: '', base_url: '' }

export function AddModelForm ({ onAdded, onMessage }: Props) {
  const [open, setOpen] = useState(false)
  const [provider, setProvider] = useState(PROVIDERS[0])
  const [fields, setFields] = useState({ ...EMPTY })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const set = (key: keyof typeof EMPTY) => (value: string) =>
    setFields((prev) => ({ ...prev, [key]: value }))

  const submit = async () => {
    const payload = {
      id: fields.id.trim(),
      provider_name: provider,
      model: fields.model.trim(),
      api_key: fields.api_key.trim(),
      base_url: fields.base_url.trim()
    }
    if (!payload.id || !payload.model || !payload.api_key) {
      setError(t('models.required'))
      return
    }
    setError('')
    setBusy(true)
    const saved = await collect('model_add', payload).catch(() => null)
    if (!saved) {
      setBusy(false)
      setError(t('models.saveCheckFields'))
      return
    }
    // 存完立刻测：连不上的模型比没有更糟 —— 它会在分析中途静默失败。
    const res = await collect('model_test', { id: payload.id }).catch(() => null)
    setBusy(false)
    if (res && res.connected) {
      onMessage(t('models.addedConnected', { id: payload.id, ms: Number(res.latency_ms) || 0 }))
    } else {
      onMessage(
        t('models.savedButFailed', {
          id: payload.id,
          error: String((res && res.error) || t('models.unknownError'))
        }),
        true
      )
    }
    // 成功后清空并收起：留着旧值容易误以为还没提交。
    setFields({ ...EMPTY })
    setOpen(false)
    await onAdded()
  }

  return (
    <div className="madd">
      <button type="button" className="wel-c" onClick={() => setOpen(!open)}>
        {open ? t('models.cancelAdd') : t('models.addCustom')}
      </button>
      {open ? (
        <div className="mform">
          <Field label={t('models.fieldId')} placeholder={t('models.fieldIdPlaceholder')}
            value={fields.id} onChange={set('id')} />
          <div className="mfield">
            <label className="mflab">Provider</label>
            <select className="sel" value={provider} onChange={(e) => setProvider(e.target.value)}>
              {PROVIDERS.map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
          </div>
          <Field label={t('models.fieldModel')} placeholder={t('models.fieldModelPlaceholder')}
            value={fields.model} onChange={set('model')} />
          {/* 密钥用 password 类型：设置面板可能在录屏或结对时打开 */}
          <Field label="API Key" placeholder="sk-…" type="password"
            value={fields.api_key} onChange={set('api_key')} />
          <Field label={t('models.fieldBaseUrl')} placeholder={t('models.fieldBaseUrlPlaceholder')}
            value={fields.base_url} onChange={set('base_url')} />
          <button type="button" className="wel-c" disabled={busy} onClick={() => void submit()}>
            {busy ? t('models.saving') : t('models.saveTest')}
          </button>
          {error ? <span className="snote err">{error}</span> : null}
        </div>
      ) : null}
    </div>
  )
}

function Field ({ label, placeholder, value, onChange, type }: {
  label: string
  placeholder: string
  value: string
  onChange: (value: string) => void
  type?: string
}) {
  return (
    <div className="mfield">
      <label className="mflab">{label}</label>
      <input
        className="mfin"
        type={type || 'text'}
        placeholder={placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  )
}
