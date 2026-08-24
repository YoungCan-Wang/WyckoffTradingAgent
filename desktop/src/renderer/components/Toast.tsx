/**
 * 全局轻提示。
 *
 * 为什么需要它：账号相关的反馈（已同步 N 项配置、已退出登录、退出失败）原来走
 * `WyckoffChat.sysLine` 塞进**对话流**。那是错的 —— 对话流是用户与模型的往来记录，
 * 「我刚登录了」不属于那段对话。而且它会永久留在记录里，下次翻历史还在，
 * 甚至会被当成上下文的一部分。
 *
 * 这类反馈的正确形态是「说完就走」：几秒后自动消失，不留痕。
 *
 * ## 为什么不用 aria-live 就够了
 *
 * `role="status"` + `aria-live="polite"` 让读屏在不打断当前朗读的前提下念出来。
 * 用 `alert` 会抢断，而这些提示都不紧急。错误态用 `assertive` —— 「退出失败」
 * 需要立刻知道，否则用户会以为已经退出了。
 */
import { useEffect, useState } from 'react'

export interface ToastMessage {
  id: number
  text: string
  error?: boolean
}

/** 单条提示的存活时长。错误留久一点：读完要有时间反应。 */
const LIFE_MS = 3200
const LIFE_MS_ERROR = 5000

export function Toast ({ items, onExpire }: { items: ToastMessage[]; onExpire: (id: number) => void }) {
  if (!items.length) return null
  return (
    <div className="toast-wrap">
      {items.map((m) => (
        <ToastItem key={m.id} item={m} onExpire={onExpire} />
      ))}
    </div>
  )
}

function ToastItem ({ item, onExpire }: { item: ToastMessage; onExpire: (id: number) => void }) {
  const [leaving, setLeaving] = useState(false)

  useEffect(() => {
    const life = item.error ? LIFE_MS_ERROR : LIFE_MS
    // 先播退场动画再摘除，否则会突然消失。
    const fade = setTimeout(() => setLeaving(true), life)
    const drop = setTimeout(() => onExpire(item.id), life + 200)
    return () => { clearTimeout(fade); clearTimeout(drop) }
  }, [item.id, item.error, onExpire])

  return (
    <div
      className={[
        'toast',
        item.error ? 'toast-err' : '',
        leaving ? 'toast-out' : ''
      ].filter(Boolean).join(' ')}
      // 错误要抢断朗读；普通提示不打扰用户正在听的内容。
      role={item.error ? 'alert' : 'status'}
      aria-live={item.error ? 'assertive' : 'polite'}
    >
      {item.text}
    </div>
  )
}
