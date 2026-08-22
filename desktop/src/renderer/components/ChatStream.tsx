/**
 * 对话流。一轮 = 用户那句话 + 助手的块列表。
 *
 * 滚动跟随只在「本来就在底部」时生效 —— 用户往上翻看历史时，新内容到达
 * 不该把他拽回底部。
 */
import { useEffect, useRef } from 'react'
import type { Turn, Block } from '../lib/chat'
import { ApprovalCardInline } from './ApprovalCardInline'
import { Markdown } from './Markdown'

const t = (key: string, params?: Record<string, string | number>) => window.WyckoffI18n.t(key, params)

interface Props {
  turns: Turn[]
  onApprovalDecided: (toolName: string) => void
}

export function ChatStream ({ turns, onApprovalDecided }: Props) {
  const endRef = useRef<HTMLDivElement>(null)
  const scroller = useRef<HTMLElement | null>(null)
  // 到达时是否贴着底部 —— 决定这次要不要跟随
  const wasAtBottom = useRef(true)

  useEffect(() => {
    scroller.current = document.getElementById('stream')
  }, [])

  useEffect(() => {
    const box = scroller.current
    if (!box) return
    if (wasAtBottom.current) box.scrollTop = box.scrollHeight
  }, [turns])

  useEffect(() => {
    const box = scroller.current
    if (!box) return
    const onScroll = () => {
      wasAtBottom.current = box.scrollHeight - box.scrollTop - box.clientHeight < 60
    }
    box.addEventListener('scroll', onScroll)
    return () => box.removeEventListener('scroll', onScroll)
  }, [])

  return (
    <>
      {turns.map((turn) => (
        <div key={turn.id}>
          {turn.user !== undefined ? (
            <div className="msg">
              <span className="av">{t('chat.you')}</span>
              <div className="bd"><p>{turn.user}</p></div>
            </div>
          ) : null}
          {turn.blocks.length || turn.live ? (
            <div className="msg a">
              <span className="av">✳</span>
              <div className="bd">
                {turn.blocks.map((b, i) => (
                  <BlockView key={i} block={b} onApprovalDecided={onApprovalDecided} />
                ))}
                {/* 还在跑但一个字都没来：给个占位，别让界面看起来卡死 */}
                {turn.live && !turn.blocks.length ? (
                  <p className="chat-wait">{t('chat.thinking')}</p>
                ) : null}
              </div>
            </div>
          ) : null}
        </div>
      ))}
      <div ref={endRef} />
    </>
  )
}

/**
 * 送去右侧面板的产物在对话里留下的卡片。
 *
 * 存在的理由是「找得回来」：原来这里是一行纯文本「已在右侧打开 →」，用户关掉
 * 页签之后没有任何入口，只能重新问一遍模型。正文存在 block 里，所以重开不需要
 * 再走一次模型，面板渲染失败也不会丢东西。
 */
function ArtifactCard ({ title, body }: { title: string; body: string }) {
  return (
    <div className="sys art-card">
      <span className="art-title">{title}</span>
      <button
        type="button"
        className="task-action"
        onClick={() => window.WyckoffApp?.openReport?.(title, body)}
      >
        {t('chat.reopen')}
      </button>
    </div>
  )
}

function BlockView (
  { block, onApprovalDecided }: { block: Block; onApprovalDecided: (n: string) => void }
) {
  switch (block.kind) {
    case 'text':
      // 模型的正文是 markdown（列表、粗体、代码、表格），原样输出会看到
      // 满屏的 ** 和 -。
      return <Markdown source={block.text} />
    case 'tool':
      return (
        <div className="tool">
          <span className="g">◈</span>
          <span className="nm">{block.display}</span>
        </div>
      )
    case 'toolError':
      return (
        <div className="sys err">
          {`${block.name || t('chat.tool')}：${block.error || t('chat.toolFailed')}`}
        </div>
      )
    case 'error':
      return <div className="sys err">{block.message || t('chat.errored')}</div>
    case 'note':
      return <div className="sys">{block.text}</div>
    case 'artifact':
      return <ArtifactCard title={block.title} body={block.body} />
    case 'approval':
      return <ApprovalCardInline event={block.event} onDecided={onApprovalDecided} />
    default:
      return null
  }
}
