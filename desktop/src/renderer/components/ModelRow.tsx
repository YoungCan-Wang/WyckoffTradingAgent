/**
 * 一个模型：身份、角色标签、连通性测试、删除。
 *
 * 只显示这个模型**还没有**的角色按钮。11 个模型时把四个动作全铺出来是 44 个
 * 按钮，真正要紧的两件事（谁是主、谁是备）会被淹掉。
 */
import { useState } from 'react'
import { collect } from '../lib/ipc'
import type { ModelEntry } from '../types'

const t = (key: string, params?: Record<string, string | number>) => window.WyckoffI18n.t(key, params)

interface Props {
  model: ModelEntry
  isDefault: boolean
  isFallback: boolean
  /** 角色或删除成功后要重读配置。 */
  onChanged: () => Promise<void> | void
}

export function ModelRow ({ model, isDefault, isFallback, onChanged }: Props) {
  const [note, setNote] = useState<{ text: string; error: boolean } | null>(null)
  const [testing, setTesting] = useState(false)

  const flash = (text: string, error: boolean) => {
    setNote({ text, error })
    setTimeout(() => setNote(null), 2400)
  }

  const setRole = async (key: 'default_model' | 'fallback_model') => {
    const res = await collect('settings_set', { key, value: model.id })
    if (!res) { flash(t('models.saveFailed'), true); return }
    await onChanged()
  }

  const runTest = async () => {
    setTesting(true)
    const res = await collect('model_test', { id: model.id }).catch(() => null)
    setTesting(false)
    if (!res) { flash(t('models.testFailed'), true); return }
    if (res.connected) flash(t('models.connected', { ms: Number(res.latency_ms) || 0 }), false)
    else flash(String(res.error || t('models.disconnected')), true)
  }

  const remove = async () => {
    if (!window.confirm(t('models.deleteConfirm', { id: model.id }))) return
    const res = await collect('model_remove', { id: model.id }).catch(() => null)
    if (!res) { flash(t('models.deleteFailed'), true); return }
    await onChanged()
  }

  const sub = [model.provider_name, model.model, model.base_url].filter(Boolean).join(' · ')

  return (
    <div className="mrow">
      <div className="minfo">
        <div className="mtitle">
          <span className="mid">{model.id}</span>
          {isDefault ? <span className="tag pri">{t('models.tagDefault')}</span> : null}
          {isFallback ? <span className="tag alt">{t('models.tagFallback')}</span> : null}
          {!model.has_key ? <span className="tag warn">{t('models.tagNoKey')}</span> : null}
        </div>
        {/* CSS 里会省略号截断，所以把完整值挂在 title 上供悬停查看 */}
        <span className="msub" title={sub}>{sub}</span>
      </div>

      <div className="macts">
        {!isDefault ? (
          <button type="button" className="mbtn" onClick={() => void setRole('default_model')}>
            {t('models.setDefault')}
          </button>
        ) : null}
        {!isFallback && !isDefault ? (
          <button type="button" className="mbtn" onClick={() => void setRole('fallback_model')}>
            {t('models.setFallback')}
          </button>
        ) : null}
        {/* 连通性测试发真实请求，所以结果可信 */}
        <button type="button" className="mbtn" disabled={testing} onClick={() => void runTest()}>
          {testing ? t('models.testing') : t('models.test')}
        </button>
        {/* 在用的模型刻意不可删：删掉正在跑的分析所依赖的模型会中途失败。先换角色。 */}
        {!isDefault && !isFallback ? (
          <button type="button" className="mbtn danger" onClick={() => void remove()}>
            {t('models.delete')}
          </button>
        ) : null}
      </div>

      {note ? <span className={note.error ? 'snote err' : 'snote'}>{note.text}</span> : null}
    </div>
  )
}
