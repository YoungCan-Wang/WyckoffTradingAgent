/**
 * 输入区的模型选择器。
 *
 * 菜单方向按实测空间决定而不是写死：欢迎页输入框在屏幕中部，向下有空间；
 * 开聊后贴到底部，下方只剩十几像素。两处都不该被裁。
 */
import { useEffect, useRef, useState } from 'react'
import { collect } from '../lib/ipc'
import type { Settings, ModelEntry } from '../types'

const t = (key: string) => window.WyckoffI18n.t(key)

const label = (m: ModelEntry) => m.model || m.id

export function ModelPicker () {
  const [data, setData] = useState<Settings | null>(null)
  const [open, setOpen] = useState(false)
  const [up, setUp] = useState(false)
  const btn = useRef<HTMLButtonElement>(null)
  const menu = useRef<HTMLDivElement>(null)

  const load = async () => {
    const res = await collect('settings_get').catch(() => null)
    if (res) setData(res as unknown as Settings)
  }
  useEffect(() => { void load() }, [])

  // 设置里改了模型 → 这里要跟着变。app.js 时代靠 refreshModels()，
  // 现在监听同一个信号。
  useEffect(() => {
    const onChanged = () => { void load() }
    window.addEventListener('wyckoff:models-changed', onChanged)
    return () => window.removeEventListener('wyckoff:models-changed', onChanged)
  }, [])

  // 点外面关掉
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (btn.current?.contains(e.target as Node)) return
      if (menu.current?.contains(e.target as Node)) return
      setOpen(false)
    }
    const onEsc = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onEsc)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onEsc)
    }
  }, [open])

  const models = (data && Array.isArray(data.models) ? data.models : []) as ModelEntry[]
  const current = models.find((m) => m.id === data?.default_model)

  const toggle = () => {
    if (!open && btn.current) {
      // 下方装不下、上方装得下才翻上去
      const r = btn.current.getBoundingClientRect()
      const need = Math.min(models.length * 44 + 60, 320)
      setUp(window.innerHeight - r.bottom < need && r.top > need)
    }
    setOpen(!open)
  }

  const pick = async (m: ModelEntry) => {
    setOpen(false)
    if (m.id === data?.default_model) return
    const res = await collect('settings_set', { key: 'default_model', value: m.id })
    if (!res) {
      window.WyckoffApp?.sysLine?.(t('composer.modelSwitchFailed'), true)
      return
    }
    await load()
    window.dispatchEvent(new Event('wyckoff:models-changed'))
  }

  return (
    <div className="mdl-pick" id="mdl-pick">
      <button
        ref={btn}
        className="mdl"
        id="mdl-btn"
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        title={current ? (current.id || '') : t('composer.noModelHint')}
        onClick={toggle}
      >
        {/* 没配过模型时不显示假名字，给一句能点的提示 */}
        <span id="mdl-name">{current ? label(current) : t('composer.noModel')}</span>
      </button>
      <div
        ref={menu}
        className={up ? 'mdl-menu up fits' : 'mdl-menu fits'}
        id="mdl-menu"
        role="listbox"
        hidden={!open}
      >
        {!models.length ? <div className="mdl-empty">{t('composer.noModelHint')}</div> : null}
        {models.map((m) => {
          const sub = [m.provider_name, m.id !== label(m) ? m.id : null].filter(Boolean).join(' · ')
          return (
            <button
              key={m.id}
              type="button"
              role="option"
              aria-selected={m.id === data?.default_model}
              className={m.id === data?.default_model ? 'mdl-o on' : 'mdl-o'}
              onClick={() => void pick(m)}
            >
              <span>{label(m)}</span>
              {sub ? <span className="msub">{sub}</span> : null}
            </button>
          )
        })}
        <button
          type="button"
          className="mdl-add"
          onClick={() => {
            setOpen(false)
            // 直接落到「模型」那一栏 —— 停在页首等于还要用户自己找一遍
            window.WyckoffApp?.openSettings?.('agent', 'models.heading')
          }}
        >
          {t('composer.addModel')}
        </button>
      </div>
    </div>
  )
}
