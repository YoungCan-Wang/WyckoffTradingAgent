/** 「通用」页：外观 + 行为两栏。 */
import { useEffect, useState } from 'react'
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
  const [update, setUpdate] = useState<Awaited<ReturnType<typeof window.wyckoff.checkUpdate>> | null>(null)

  useEffect(() => {
    let alive = true
    void window.wyckoff.checkUpdate().then((result) => { if (alive) setUpdate(result) })
    return () => { alive = false }
  }, [])

  /**
   * 改一个设置项，并让命令式那侧也跟上。
   *
   * shell.js 自己存了一份配置快照（setData）和一份 sendOnEnter。以前这里只调
   * React 的 applyAppearance（纯 DOM 操作），那份快照就一直是旧的，于是：
   *
   * - 「Cmd+Enter 发送」改了不生效 —— 发送键的判断读的是 shell.js 那份；
   * - OS 深浅色一变，osDark 监听器拿着过期快照重新应用，把用户刚选的主题**打回**。
   *
   * 所以每次成功保存都通知一次 shell.js 重新读配置。失败则回滚 DOM。
   */
  const saveAndApply = async <K extends keyof Settings>(key: K, value: Settings[K]) => {
    applyAppearance({ ...data, [key]: value })
    const ok = await save(key, value)
    if (!ok) {
      applyAppearance(data)
      return
    }
    await window.WyckoffShell?.loadAppearance?.()
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
          onPick={(v) => void saveAndApply('desktop_send_on_enter', v)}
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

      <SecHead k="settings.updates" />

      <Row
        label={t('updates.version')}
        hint={update ? t('updates.current', { version: update.currentVersion }) : t('updates.checking')}
      >
        {update?.updateAvailable && update.releaseUrl ? (
          <button
            type="button"
            className="b pri"
            onClick={() => void window.wyckoff.openRelease(update.releaseUrl!)}
          >
            {t('updates.download', { version: update.latestVersion || '' })}
          </button>
        ) : (
          <span className={update?.ok ? 'ok' : 'miss'}>
            {update?.ok ? t('updates.latest') : update ? t('updates.failed') : t('updates.checking')}
          </span>
        )}
      </Row>
    </>
  )
}
