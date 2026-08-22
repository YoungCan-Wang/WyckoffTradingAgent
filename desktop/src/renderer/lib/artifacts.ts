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
 * 把后端事件解析成产物；不是产物事件则返回 null。
 *
 * 刻意宽松：字段缺失时返回 null 而不是抛错。事件来自另一个进程，
 * 版本不一致时前端不该崩 —— 少一张卡片好过整个对话流挂掉。
 */
export function parseArtifactEvent (event: Record<string, unknown>): ChatArtifact | null {
  if (String(event.type || '') !== 'chat_artifact') return null
  // 注意读的是 artifact_id 而不是 id：传输层会把 event.id 覆盖成请求流 id。
  const id = String(event.artifact_id || '')
  const kind = String(event.kind || '')
  if (!id || !(ARTIFACT_KINDS as readonly string[]).includes(kind)) return null
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
    id: `${turnId}:report`,
    kind: 'report',
    title,
    status: 'ready',
    payload: { body }
  }
}
