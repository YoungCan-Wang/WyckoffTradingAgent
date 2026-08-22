/**
 * 会话区：欢迎态 → 对话态。
 *
 * 输入区在两个状态下是同一个组件的两次放置（欢迎页居中、对话时贴底），
 * 草稿文本提到这里，所以切换状态不会丢已经打的字。
 */
import { useEffect, useState } from 'react'
import { useChat } from '../lib/useChat'
import { ChatStream } from './ChatStream'
import { Composer } from './Composer'
import { Welcome } from './Welcome'

const t = (key: string) => window.WyckoffI18n.t(key)

export function ChatView () {
  const [ready, setReady] = useState(false)
  const [draft, setDraft] = useState('')
  const [sendOnEnter, setSendOnEnter] = useState(true)
  const chat = useChat(ready)

  // 桥没 ready 时禁用发送，避免请求在重启或停止阶段静默失败。
  useEffect(() => {
    let alive = true
    void window.wyckoff.status().then((s) => {
      if (alive) setReady(s.state === 'ready')
    })
    const off = window.wyckoff.onStatus((s) => {
      if (s.state !== 'log') setReady(s.state === 'ready')
    })
    return () => { alive = false; off() }
  }, [])

  // 「回车发送」是设置项，改了要立刻生效。
  useEffect(() => {
    const read = () => {
      const v = window.WyckoffApp?.getSendOnEnter?.()
      if (typeof v === 'boolean') setSendOnEnter(v)
    }
    read()
    window.addEventListener('wyckoff:settings-changed', read)
    return () => window.removeEventListener('wyckoff:settings-changed', read)
  }, [])

  // 让设置、退出登录等外壳动作能往对话流里写系统提示。
  useEffect(() => {
    window.WyckoffChat = {
      sysLine: chat.sysLine,
      newAnalysis: async () => {
        const reset = await chat.reset()
        if (reset) setDraft('')
        return reset
      }
    }
    return () => { delete window.WyckoffChat }
  }, [chat.sysLine, chat.reset])

  const send = () => {
    const text = draft
    if (!text.trim()) return
    setDraft('')
    void chat.send(text)
  }

  if (!chat.started) {
    return (
      <Welcome
        draft={draft}
        onDraft={setDraft}
        onSend={send}
        busy={chat.busy}
        ready={ready}
        sendOnEnter={sendOnEnter}
      />
    )
  }

  return (
    <>
      <div className="inner" id="stream-inner">
        <ChatStream
          turns={chat.turns}
          artifacts={chat.artifacts}
          onApprovalDecided={chat.invalidateOnTool}
          onOpenArtifact={chat.openArtifact}
        />
      </div>
      {/*
        输入区钉在会话区底部。
        用 fixed-to-bottom 的 flex 项而不是 sticky：这两块都是滚动容器
        (#stream) 的直接子元素，消息不满一屏时 sticky 不生效，输入框会跟在
        最后一条消息后面浮在中间，下方留一大片空白。
      */}
      <div className="comp-bottom">
        <Composer
          value={draft}
          onChange={setDraft}
          onSend={send}
          busy={chat.busy}
          ready={ready}
          sendOnEnter={sendOnEnter}
        />
        {!ready ? <p className="chat-offline">{t('chat.offline')}</p> : null}
      </div>
    </>
  )
}
