/**
 * 设置状态。读一次、每次改动写一个 key，并把「已保存 / 保存失败」
 * 挂在对应行上。
 *
 * 乐观更新：本地先改，再发 IPC。失败时回滚并提示 —— 不回滚的话界面会
 * 显示一个后端并不认的值，下次重开设置又变回去，看起来像随机丢设置。
 */
import { useCallback, useEffect, useState } from 'react'
import { collect } from './ipc'
import type { Settings } from '../types'

const t = (key: string, params?: Record<string, string | number>) => window.WyckoffI18n.t(key, params)

export function useSettings () {
  const [data, setData] = useState<Settings | null>(null)
  const [loading, setLoading] = useState(true)
  // key -> 提示文本；保存反馈按行显示，2 秒后自己消失。
  const [notes, setNotes] = useState<Record<string, { text: string; error: boolean }>>({})

  const reload = useCallback(async () => {
    const res = await collect('settings_get')
    setData(res ? (res as unknown as Settings) : null)
    setLoading(false)
  }, [])

  useEffect(() => { void reload() }, [reload])

  const flash = useCallback((key: string, text: string, error: boolean) => {
    setNotes((prev) => ({ ...prev, [key]: { text, error } }))
    setTimeout(() => {
      setNotes((prev) => {
        const next = { ...prev }
        delete next[key]
        return next
      })
    }, 2000)
  }, [])

  const save = useCallback(async <K extends keyof Settings>(key: K, value: Settings[K]) => {
    const previous = data ? data[key] : undefined
    setData((prev) => (prev ? { ...prev, [key]: value } : prev))
    const res = await collect('settings_set', { key: String(key), value: value as unknown })
    if (res) {
      flash(String(key), t('common.saved'), false)
      return true
    }
    // 回滚：让界面回到后端实际认可的值。
    setData((prev) => (prev && previous !== undefined ? { ...prev, [key]: previous } : prev))
    flash(String(key), t('common.saveFailed'), true)
    return false
  }, [data, flash])

  // flash 也暴露出去：数字输入越界这类校验失败不经过 save，但仍要在行上提示。
  return { data, loading, notes, save, reload, flash }
}
