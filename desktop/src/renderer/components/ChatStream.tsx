/**
 * 对话流。一轮 = 用户那句话 + 助手的块列表。
 *
 * 滚动跟随只在「本来就在底部」时生效 —— 用户往上翻看历史时，新内容到达
 * 不该把他拽回底部。
 */
import { useEffect, useRef } from 'react'
import type { Turn, Block } from '../lib/chat'
import type { ChatArtifact } from '../lib/artifacts'
import { ApprovalCardInline } from './ApprovalCardInline'
import { Markdown } from './Markdown'

const t = (key: string, params?: Record<string, string | number>) => window.WyckoffI18n.t(key, params)

interface Props {
  turns: Turn[]
  /** 本会话的产物 —— 按轮次筛出来渲染成可点的卡片。 */
  artifacts?: ChatArtifact[]
  onOpenArtifact?: (artifact: ChatArtifact) => void
  onApprovalDecided: (toolName: string) => void
}

export function ChatStream ({ turns, artifacts, onApprovalDecided, onOpenArtifact }: Props) {
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
                {(artifacts || [])
                  .filter((a) => a.id.startsWith(`${turn.id}:`))
                  .map((a) => (
                    <ArtifactChip key={a.id} artifact={a} onOpen={onOpenArtifact} />
                  ))}
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
/**
 * 产物在对话里的卡片 —— **三种 kind 都要有**。
 *
 * 为什么必需：一轮只自动展开第一个产物,后续的只进注册表；而任何产物关掉页签后
 * 也需要重开入口。原来只渲染 kline，于是 `save_report` / `render_dashboard`
 * 产出的东西在对话里没有任何痕迹 —— 关掉就找不回来了。
 *
 * 这是同一个疏漏犯第二次：我给 K 线修过「产物没有入口」，加新 kind 时又只顾了
 * kline。所以这里按 kind 查标签而不是写死，新增 kind 只需加一条 KIND_LABEL。
 */
const KIND_LABEL: Record<string, string> = {
  kline: 'artifact.chart',
  report: 'artifact.reportKind',
  dashboard: 'artifact.panel'
}

function ArtifactChip (
  { artifact, onOpen }: { artifact: ChatArtifact; onOpen?: (a: ChatArtifact) => void }
) {
  const failed = artifact.status === 'failed'
  return (
    <div className={failed ? 'sys err art-card' : 'sys art-card'}>
      <span className="art-title">{artifact.title}</span>
      <span>{failed ? t('artifact.failed') : t(KIND_LABEL[artifact.kind] || 'artifact.chart')}</span>
      {failed ? null : (
        <button type="button" className="task-action" onClick={() => onOpen?.(artifact)}>
          {t('chat.reopen')}
        </button>
      )}
    </div>
  )
}

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
