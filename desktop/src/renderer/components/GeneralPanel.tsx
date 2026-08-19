/** 「通用」页：外观 + 行为两栏。 */
import { Row, Seg, Range, SecHead } from './Rows'
import { applyAppearance, previewFontScale } from '../lib/appearance'
import type { Settings } from '../types'

const t = (key: string, params?: Record<string, string | number>) => window.WyckoffI18n.t(key, params)

interface Props {
  data: Settings
  notes: Record<string, { text: string; error: boolean }>
  save: <K extends keyof Settings>(key: K, value: Settings[K]) => Promise<boolean>
}

export function GeneralPanel ({ data, notes, save }: Props) {
  const note = (key: keyof Settings) => notes[String(key)]

  // 外观类改动要立刻落到 DOM，不能等下一次读配置。
  const saveAndApply = async <K extends keyof Settings>(key: K, value: Settings[K]) => {
    applyAppearance({ ...data, [key]: value })
    const ok = await save(key, value)
    if (!ok) applyAppearance(data)
  }

  const i18n = window.WyckoffI18n
  const langChoices = i18n.available().map((entry) => {
    // available() 历史上返回过字符串数组，也返回过 {code,label}；两种都兼容。
    const code = typeof entry === 'string' ? entry : entry.code
    return [code, t(`lang.${code}`)] as [string, string]
  })

  return (
    <>
      <SecHead k="settings.appearance" />

      <Row label={t('appearance.language')} hint={t('appearance.languageHint')}>
        <Seg
          value={i18n.getLang()}
          choices={langChoices}
          onPick={(code) => i18n.setLang(code, { persist: true })}
        />
      </Row>

      <Row
        label={t('appearance.theme')}
        hint={t('appearance.themeHint')}
        note={note('desktop_appearance')?.text}
        noteIsError={note('desktop_appearance')?.error}
      >
        <Seg
          value={data.desktop_appearance}
          choices={[
            ['system', t('appearance.themeSystem')],
            ['light', t('appearance.themeLight')],
            ['dark', t('appearance.themeDark')]
          ]}
          onPick={(v) => void saveAndApply('desktop_appearance', v)}
        />
      </Row>

      <Row
        label={t('appearance.font')}
        note={note('desktop_font_family')?.text}
        noteIsError={note('desktop_font_family')?.error}
      >
        <Seg
          value={data.desktop_font_family}
          choices={[['sans', t('appearance.fontSans')], ['serif', t('appearance.fontSerif')]]}
          onPick={(v) => void saveAndApply('desktop_font_family', v)}
        />
      </Row>

      <Row
        label={t('appearance.density')}
        note={note('desktop_density')?.text}
        noteIsError={note('desktop_density')?.error}
      >
        <Seg
          value={data.desktop_density}
          choices={[['cozy', t('appearance.densityCozy')], ['compact', t('appearance.densityCompact')]]}
          onPick={(v) => void saveAndApply('desktop_density', v)}
        />
      </Row>

      <Row
        label={t('appearance.fontScale')}
        hint={`${data.desktop_font_scale}%`}
        note={note('desktop_font_scale')?.text}
        noteIsError={note('desktop_font_scale')?.error}
      >
        {/* 边界与步长必须与 vanilla 版一致：80–140，步长 5。 */}
        <Range
          value={data.desktop_font_scale}
          min={80}
          max={140}
          step={5}
          onPreview={previewFontScale}
          onCommit={(v) => void saveAndApply('desktop_font_scale', v)}
        />
      </Row>

      <SecHead k="settings.behavior" />

      <Row
        label={t('behavior.sendMode')}
        hint={t('behavior.sendHint')}
        note={note('desktop_send_on_enter')?.text}
        noteIsError={note('desktop_send_on_enter')?.error}
      >
        <Seg
          value={data.desktop_send_on_enter}
          choices={[[true, t('behavior.sendEnter')], [false, t('behavior.sendCmdEnter')]]}
          onPick={(v) => void save('desktop_send_on_enter', v)}
        />
      </Row>

      <Row
        label={t('behavior.motion')}
        note={note('desktop_reduce_motion')?.text}
        noteIsError={note('desktop_reduce_motion')?.error}
      >
        <Seg
          value={data.desktop_reduce_motion}
          choices={[[false, t('behavior.motionNormal')], [true, t('behavior.motionReduced')]]}
          onPick={(v) => void saveAndApply('desktop_reduce_motion', v)}
        />
      </Row>
    </>
  )
}
