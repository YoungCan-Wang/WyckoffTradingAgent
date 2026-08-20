/**
 * 对话流的状态模型。
 *
 * vanilla 版是增量改 DOM（`textContent += delta`）。React 里不能这么干，
 * 所以把一轮对话建模成「有序的块列表」：文字增量追加到最后一个同类块上，
 * 工具行、错误、审批各自是独立块。顺序即到达顺序 —— 工具在文字中间出现时，
 * 界面上也该在那个位置。
 */

/** 一轮里的一个块。type 决定怎么渲染。 */
export type Block =
  | { kind: 'thinking'; text: string }
  | { kind: 'text'; text: string }
  | { kind: 'tool'; name: string; display: string }
  | { kind: 'toolError'; name: string; error: string }
  | { kind: 'approval'; event: Record<string, unknown> }
  | { kind: 'error'; message: string }
  | { kind: 'note'; text: string }

export interface Turn {
  id: string
  /** 用户那句话。助手轮为空。 */
  user?: string
  blocks: Block[]
  /** 还在跑 = 显示光标/禁用发送。 */
  live: boolean
  /** 这一轮画过的图，end 时要刷新一次（标注是在图建好之后写的）。 */
  drewCharts?: string[]
}

/** 追加一个块；文字类的合并到末尾同类块上。 */
export function pushBlock (blocks: Block[], next: Block): Block[] {
  const last = blocks[blocks.length - 1]
  if (
    last &&
    (next.kind === 'text' || next.kind === 'thinking') &&
    last.kind === next.kind
  ) {
    // 合并而不是新建：每个 delta 一个块会产出几百个 <p>。
    const merged = { ...last, text: last.text + next.text } as Block
    return [...blocks.slice(0, -1), merged]
  }
  return [...blocks, next]
}

/**
 * 长而结构化的回复更适合当文档看，而不是聊天气泡。
 * 判定与 vanilla 版一致：够长 且 有标题或表格。
 */
export function looksLikeReport (text?: string): boolean {
  if (!text || text.length < 400) return false
  return /^#{1,3}\s+\S/m.test(text) || /^\|.*\|$/m.test(text)
}

/** 报告标题取第一个一级/二级标题，过长截断。 */
export function reportTitle (text: string, fallback: string): string {
  const heading = text.match(/^#\s+(.+)$/m) || text.match(/^##\s+(.+)$/m)
  const raw = heading ? heading[1].trim() : fallback
  return raw.length > 18 ? `${raw.slice(0, 18)}…` : raw
}

/** 会改动持仓的工具 —— 跑了它们，持仓缓存就是脏的。 */
const PORTFOLIO_WRITE_TOOLS = new Set(['update_portfolio', 'set_stop_loss', 'record_trade_fill'])
export const isPortfolioWriteTool = (name?: string) =>
  PORTFOLIO_WRITE_TOOLS.has(String(name || ''))

/**
 * 把一条事件并进某一轮。返回新的 Turn（不改原对象）。
 *
 * 不认识的事件类型原样返回 —— 后端加了新事件不该让界面崩，也不该凭空造块。
 */
export function applyEvent (turn: Turn, event: Record<string, unknown>): Turn {
  const type = String(event.type || '')
  const str = (v: unknown) => String(v || '')

  switch (type) {
    case 'thinking_delta':
      return { ...turn, blocks: pushBlock(turn.blocks, { kind: 'thinking', text: str(event.text) }) }
    case 'text_delta':
      return { ...turn, blocks: pushBlock(turn.blocks, { kind: 'text', text: str(event.text) }) }
    case 'tool_start':
      return {
        ...turn,
        blocks: pushBlock(turn.blocks, {
          kind: 'tool',
          name: str(event.name),
          display: str(event.display_name || event.name || 'tool')
        })
      }
    case 'tool_error':
      return {
        ...turn,
        blocks: pushBlock(turn.blocks, {
          kind: 'toolError',
          name: str(event.name),
          error: str(event.error)
        })
      }
    case 'approval_pending':
      return { ...turn, blocks: pushBlock(turn.blocks, { kind: 'approval', event }) }
    case 'error':
      return { ...turn, blocks: pushBlock(turn.blocks, { kind: 'error', message: str(event.message) }) }
    default:
      return turn
  }
}

/** 这一轮最终的正文 —— done 事件带的优先，否则拼已收到的文字块。 */
export function finalText (turn: Turn, doneText?: string): string {
  if (doneText) return doneText
  return turn.blocks.filter((b) => b.kind === 'text').map((b) => (b as { text: string }).text).join('')
}
