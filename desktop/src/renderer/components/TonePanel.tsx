/** 「回复语气」栏：五个预设 + 自定义文本框。 */
import { useState } from 'react'
import type { Settings } from '../types'
import { SecHead } from './Rows'

const t = (key: string) => window.WyckoffI18n.t(key)

// id -> [标签 key, 说明 key]。标签在渲染时才解析，语言切换后会跟着变，
// 而不是冻结在加载时的语言。
const TONES: Array<[string, string, string]> = [
  ['default', 'tone.default', 'tone.defaultDesc'],
  ['brief', 'tone.brief', 'tone.briefDesc'],
  ['detailed', 'tone.detailed', 'tone.detailedDesc'],
  ['evidence', 'tone.evidence', 'tone.evidenceDesc'],
  ['custom', 'tone.custom', 'tone.customDesc']
]

const CUSTOM_MAX = 600

interface Props {
  data: Settings
  notes: Record<string, { text: string; error: boolean }>
  save: <K extends keyof Settings>(key: K, value: Settings[K]) => Promise<boolean>
}

export function TonePanel ({ data, notes, save }: Props) {
  // 自定义文本是草稿：改动到点「保存」才落盘，与 vanilla 版一致。
  const [draft, setDraft] = useState(data.desktop_tone_custom || '')
  const toneNote = notes.desktop_tone
  const customNote = notes.desktop_tone_custom

  return (
    <>
      <SecHead k="settings.tone" />
      <p className="dlg-sub">{t('tone.note')}</p>

      <div className="tone-l">
        {TONES.map(([value, labelKey, descKey]) => (
          <button
            key={value}
            type="button"
            className={value === data.desktop_tone ? 'tone-o on' : 'tone-o'}
            onClick={() => void save('desktop_tone', value)}
          >
            <span className="tone-n">{t(labelKey)}</span>
            <span className="tone-d">{t(descKey)}</span>
          </button>
        ))}
      </div>
      {toneNote ? (
        <span className={toneNote.error ? 'snote err' : 'snote'}>{toneNote.text}</span>
      ) : null}

      {/* 只有选了「自定义」才显示输入框 —— 与 vanilla 的 syncCustom 同义 */}
      {data.desktop_tone === 'custom' ? (
        <div className="tone-c">
          <textarea
            className="tone-t"
            rows={4}
            maxLength={CUSTOM_MAX}
            placeholder={t('tone.customPlaceholder')}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
          />
          <button type="button" className="wel-c" onClick={() => void save('desktop_tone_custom', draft)}>
            {t('tone.saveCustom')}
          </button>
          {customNote ? (
            <span className={customNote.error ? 'snote err' : 'snote'}>{customNote.text}</span>
          ) : null}
        </div>
      ) : null}
    </>
  )
}
