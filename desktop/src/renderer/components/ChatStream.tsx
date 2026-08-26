/**
 * 对话流。一轮 = 用户那句话 + 助手的块列表。
 *
 * 滚动跟随只在「本来就在底部」时生效 —— 用户往上翻看历史时，新内容到达
 * 不该把他拽回底部。
 */
import { useEffect, useRef, useState } from 'react'
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
            <div className="msg u">
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
                {/* 等待提示。两种情况都要有：
                    - 一个字都没来（原来只覆盖这种）
                    - 工具跑完、模型继续想下一步 —— 这时 blocks 已有内容，
                      原来占位就消失了，界面又静下来看着像卡住
                    文案优先用后端的阶段提示（「正在分析」），比泛泛的
                    「正在思考」更具体；多轮时带轮次，那是「还在推进」的直接证据。 */}
                {turn.live && (!turn.blocks.length || turn.stage) ? (
                  <p className="chat-wait">{waitLabel(turn)}</p>
                ) : null}
                {/* 回复结束后的操作栏。跑的过程中不显示 —— 复制一段还在变的
                    文本没有意义,时间也还没定。 */}
                {!turn.live && turn.blocks.some((b) => b.kind === 'text') ? (
                  <ReplyFoot turn={turn} />
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
 * 回复下方的操作栏:复制 + 时间。
 *
 * 默认只显示时间(很淡),复制按钮 hover 到整条回复上才出现 —— 一直挂着会让
 * 长会话看起来到处是按钮。但**键盘可达**:focus-visible 时也显示,否则
 * 只能靠鼠标的功能对键盘用户等于不存在。
 */
function ReplyFoot ({ turn }: { turn: Turn }) {
  const [copied, setCopied] = useState(false)

  const copy = async () => {
    // 只复制正文,不含工具行/审批卡片 —— 用户要的是回答本身,
    // 粘出去带一串「✓ 持仓」是噪音。
    const text = turn.blocks
      .filter((b) => b.kind === 'text')
      .map((b) => (b as { text: string }).text)
      .join('\n\n')
      .trim()
    if (!text) return
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      // 复制成功要有反馈,否则用户不知道成没成,会再点一次。
      window.setTimeout(() => setCopied(false), 1600)
    } catch {
      // 剪贴板被拒(权限/焦点问题):不弹错,静默失败比错误提示更不碍事 ——
      // 用户还可以选中后右键复制。
    }
  }

  return (
    <div className="reply-foot">
      <button
        type="button"
        className="reply-copy"
        onClick={copy}
        aria-label={t('chat.copyReply')}
      >
        {copied ? t('chat.copied') : t('chat.copy')}
      </button>
      {turn.at ? <span className="reply-time">{formatTime(turn.at)}</span> : null}
    </div>
  )
}

/**
 * 回复时间。同一天只显示时分;跨天补上日期 —— 昨天的对话显示「14:30」
 * 会让人误以为是刚才。
 */
function formatTime (ts: number): string {
  const d = new Date(ts)
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  const now = new Date()
  const sameDay = d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate()
  if (sameDay) return hm
  return `${d.getMonth() + 1}/${d.getDate()} ${hm}`
}

/**
 * 等待中显示什么。
 *
 * 优先后端给的阶段文案:它知道自己在干什么（「正在分析」),而前端只能说
 * 「正在思考」。第 2 轮起带上轮次 —— 长任务里那是唯一能说明「还在推进、
 * 不是卡住」的信息。
 */
function waitLabel (turn: Turn): string {
  const stage = String(turn.stage || '').trim()
  const base = stage || t('chat.thinking')
  const round = Number(turn.round) || 0
  return round > 1 ? t('chat.waitRound', { label: base, round }) : base
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
      // 在跑就转圈，跑完换对勾。原来两种状态长得一模一样，一串工具行躺着不动
      // 分不清是在干活还是卡住了。
      return (
        <div className={block.done ? 'tool done' : 'tool running'}>
          <span className="g" aria-hidden="true">{block.done ? '✓' : '◈'}</span>
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
