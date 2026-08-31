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
  | { kind: 'text'; text: string }
  | { kind: 'tool'; name: string; display: string; done?: boolean }
  | { kind: 'toolError'; name: string; error: string }
  // 就地确认 / 就地提问。都是「这一轮在等你回一句」，等到答复才继续。
  | { kind: 'confirm'; event: Record<string, unknown> }
  | { kind: 'question'; event: Record<string, unknown> }
  | { kind: 'error'; message: string }
  | { kind: 'note'; text: string }
  /**
   * 送去右侧面板的产物（目前只有报告）。
   *
   * **必须带着 body**：原来这条路径是 `blocks.filter(b => b.kind !== 'text')`
   * 把正文整块滤掉、只留一句「已在右侧打开」。于是 openReport 一旦失败（渲染
   * 抛异常、面板被关），模型生成的完整正文就**彻底没了** —— 那一轮回复只剩一
   * 行提示，重开无门，只能重新问一遍。
   *
   * 留着 body 还顺带解决了「关掉页签后找不回来」：卡片上的按钮可以拿它重开。
   */
  | { kind: 'artifact'; artifactKind: 'report'; title: string; body: string }

export interface Turn {
  id: string
  /** 用户那句话。助手轮为空。 */
  user?: string
  blocks: Block[]
  /** 还在跑 = 显示光标/禁用发送。 */
  live: boolean
  /** 这一轮画过的图，end 时要刷新一次（标注是在图建好之后写的）。 */
  drewCharts?: string[]
  /** 当前阶段文案（后端给的「正在分析」之类）。空 = 没有进行中的阶段。 */
  stage?: string
  /** 第几轮。多轮时显示出来，让用户知道它在继续推进而不是卡住。 */
  round?: number
  /** 这一轮开始的本地时间戳。回复下方显示「几点」，也用于跨天时补日期。 */
  at?: number
}

/** 追加一个块；文字类的合并到末尾同类块上。 */
/**
 * 把最后一个同名的未完成工具块标成已完成。
 *
 * 从后往前找:同一轮里可能连着调同一个工具（例如两只票各查一次持仓）,
 * 从前往后会把第二次的结果记到第一次头上,于是第一行提前变对勾、
 * 第二行永远转圈。
 *
 * 找不到就原样返回 —— 有些工具的 tool_result 可能先于 tool_start 到达(重试
 * 路径),那时不该凭空造一个块出来。
 */
export function markToolDone (blocks: Block[], name: string): Block[] {
  for (let i = blocks.length - 1; i >= 0; i--) {
    const b = blocks[i]
    if (b.kind === 'tool' && b.name === name && !b.done) {
      const out = blocks.slice()
      out[i] = { ...b, done: true }
      return out
    }
  }
  return blocks
}

export function pushBlock (blocks: Block[], next: Block): Block[] {
  const last = blocks[blocks.length - 1]
  if (
    last &&
    next.kind === 'text' &&
    last.kind === next.kind
  ) {
    // 合并而不是新建：每个 delta 一个块会产出几百个 <p>。
    const merged = { ...last, text: last.text + next.text } as Block
    return [...blocks.slice(0, -1), merged]
  }
  return [...blocks, next]
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
      // 内部推理不是用户正文。即使旧后端仍发该事件，也绝不渲染。
      return turn
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
    case 'stage_start':
      // 「正在分析」这类阶段提示。模型生成工具调用要十几秒,这段时间原来只有
      // 一句静止的「正在思考…」—— 有了阶段文案和轮次,等待才有进展感。
      return { ...turn, stage: str(event.message), round: Number(event.round) || 0 }
    case 'stage_done':
      // 阶段结束就清掉:留着会让「正在分析」一直挂在已经进入工具执行的轮次上。
      return { ...turn, stage: '' }
    case 'tool_result':
      // 把对应的工具块标成完成。界面据此把转圈换成对勾 —— 原来 tool_result
      // 根本不过 IPC 白名单,所以「在跑」和「跑完」长得一模一样,那两行躺着不动
      // 看着就像卡死了。
      //
      // 从后往前找第一个同名未完成块:同一轮可能连着调同一个工具（比如两只票
      // 各查一次持仓）,从前往后会把第二次的结果记到第一次头上。
      return { ...turn, blocks: markToolDone(turn.blocks, str(event.name)) }
    case 'tool_error':
      return {
        ...turn,
        blocks: pushBlock(turn.blocks, {
          kind: 'toolError',
          name: str(event.name),
          error: str(event.error)
        })
      }
    case 'confirm_request':
      return { ...turn, blocks: pushBlock(turn.blocks, { kind: 'confirm', event }) }
    case 'question_request':
      return { ...turn, blocks: pushBlock(turn.blocks, { kind: 'question', event }) }
    case 'waiting_for_user':
      // 等待期间的心跳（喂 bridge 的静默看门狗）。卡片已经在流里了，不用再画。
      return turn
    case 'error':
      return { ...turn, blocks: pushBlock(turn.blocks, { kind: 'error', message: str(event.message) }) }
    default:
      return turn
  }
}

