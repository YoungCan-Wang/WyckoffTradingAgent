/**
 * 会话产物：聊天里生成的、右侧面板能打开的东西。
 *
 * 为什么要建模成数据（而不是继续「工具跑完顺手调 openKline」）：
 * - 产物需要**可寻址**，否则关掉页签就找不回来（K 线原来连一行痕迹都不留）。
 * - 产物需要**可去重**，否则同一次工具调用重复投影会开两个页签。
 * - 「什么时候自动展开」是一条有状态的决策，散在副作用里没法测。
 *
 * 命名注意：`cli/ipc/artifacts.py` 里的 `Artifact` 是**磁盘上的报告文件**
 * （报告库）。这里的 ChatArtifact 属于某一轮对话、按 turnId:callId 寻址。
 * 两者是不同的东西，共用一个名字会让 artifact 在代码里同时指两件事。
 */

/**
 * 产物类型的**唯一**清单。
 *
 * 类型和运行期校验都从这里派生 —— 加新 kind 时只改这一处。
 * 原来校验里写着 `kind !== 'kline' && kind !== 'report'`，加 dashboard 时
 * 我改了后端、shell、宽度表，却漏了那一行 —— 事件被静默丢掉，面板压根不开，
 * 而且没有任何报错。这种「加一半」正是重复清单的必然结果。
 */
export const ARTIFACT_KINDS = ['kline', 'report', 'dashboard'] as const

export type ArtifactKind = (typeof ARTIFACT_KINDS)[number]
export type ArtifactStatus = 'ready' | 'failed'

export interface ChatArtifact {
  id: string
  kind: ArtifactKind
  title: string
  status: ArtifactStatus
  /** 打开它所需的最小信息。K 线是 symbol/timeframe，报告是正文。 */
  payload: Record<string, unknown>
}

/**
 * 产物 id = `轮次:调用`。
 *
 * **轮次用前端已知的 IPC 流 id,不是后端自己编的序号。** 后端拿不到传输层的
 * 请求 id（那在 stdio 层注入），它曾经自己编过一个 `turn-N` —— 而前端的
 * `turn.id` 是流 id（数字，如 '17'）。两个命名空间，`startsWith('17:')` 恒为
 * 假，于是对话里的产物卡片**一张都不显示**、报告去重也永不生效。
 *
 * 两侧的单测当时全绿：它们各自造 id 各自验，从没让真实的两端拼一次。
 */
export function artifactId (turnId: string, callId: string): string {
  // callId 缺失时仍带轮次前缀：宁可同轮多次调用互相覆盖，也不能产出前缀对不上
  // 的 id —— 后者会让卡片静默消失，比覆盖难查得多。
  return `${turnId}:${callId || 'call'}`
}

/**
 * 把后端事件解析成产物；不是产物事件则返回 null。
 *
 * 刻意宽松：字段缺失时返回 null 而不是抛错。事件来自另一个进程，
 * 版本不一致时前端不该崩 —— 少一张卡片好过整个对话流挂掉。
 */
export function parseArtifactEvent (event: Record<string, unknown>): ChatArtifact | null {
  if (String(event.type || '') !== 'chat_artifact') return null
  // event.id 是传输层塞的请求流 id —— 它正是这一轮的 turn.id。
  // 后端只给 artifact_call_id，轮次由这里拼上。
  const turnId = String(event.id || '')
  const kind = String(event.kind || '')
  if (!turnId || !(ARTIFACT_KINDS as readonly string[]).includes(kind)) return null
  const id = artifactId(turnId, String(event.artifact_call_id || ''))
  const status = String(event.status || '') === 'failed' ? 'failed' : 'ready'
  const payload = event.payload
  return {
    id,
    kind: kind as ArtifactKind,
    title: String(event.title || ''),
    status,
    payload: (payload && typeof payload === 'object' ? payload : {}) as Record<string, unknown>
  }
}

/**
 * 按 id 合并进列表：已存在则替换（状态会从 ready 变 failed，或反过来重画），
 * 不存在则追加。
 *
 * 用「替换 + 保持原位置」而不是「删掉再追加」：产物在列表里的顺序应该反映
 * 它**第一次**出现的时间。重画一张图不该让它跳到最后。
 */
export function mergeArtifact (list: ChatArtifact[], next: ChatArtifact): ChatArtifact[] {
  const at = list.findIndex((a) => a.id === next.id)
  if (at === -1) return [...list, next]
  const copy = [...list]
  copy[at] = next
  return copy
}

/** 报告不走工具，所以由前端在 done 时合成一个产物。 */
export function reportArtifact (turnId: string, title: string, body: string): ChatArtifact {
  return {
    id: artifactId(turnId, 'report'),
    kind: 'report',
    title,
    status: 'ready',
    payload: { body }
  }
}
