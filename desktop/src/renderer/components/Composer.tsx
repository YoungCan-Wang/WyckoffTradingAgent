/**
 * 输入区。欢迎态时在卡片中间，开聊后移到底部 —— vanilla 版是把同一个 DOM
 * 节点搬过去（为了保住监听器和已输入的文字），React 里靠条件放置即可。
 */
import { useEffect, useRef } from 'react'
import { ModelPicker } from './ModelPicker'

const t = (key: string) => window.WyckoffI18n.t(key)

interface Props {
  value: string
  onChange: (v: string) => void
  onSend: () => void
  busy: boolean
  ready: boolean
  sendOnEnter: boolean
}

export function Composer ({ value, onChange, onSend, busy, ready, sendOnEnter }: Props) {
  const box = useRef<HTMLTextAreaElement>(null)

  // 高度跟着内容长 —— 固定行数会让长提示词看不全。
  useEffect(() => {
    const node = box.current
    if (!node) return
    node.style.height = 'auto'
    node.style.height = `${Math.min(node.scrollHeight, 220)}px`
  }, [value])

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== 'Enter') return
    // 「回车发送」是设置项。关掉时用 ⌘/Ctrl+回车发送，回车换行。
    const wantsSend = sendOnEnter ? !e.shiftKey : (e.metaKey || e.ctrlKey)
    if (!wantsSend) return
    e.preventDefault()
    onSend()
  }

  return (
    <div className="comp" id="comp">
      <textarea
        ref={box}
        id="input"
        rows={1}
        placeholder={t('composer.placeholder')}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={onKeyDown}
      />
      <div className="cr">
        <ModelPicker />
        <button
          className="send"
          id="btn-send"
          type="button"
          aria-label={t('action.send')}
          disabled={busy || !ready || !value.trim()}
          onClick={onSend}
        >↑</button>
      </div>
    </div>
  )
}
