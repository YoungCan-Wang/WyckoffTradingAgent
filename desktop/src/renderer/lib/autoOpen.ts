/**
 * 自动展开决策。
 *
 * 这是整套产物机制里唯一不能靠「代码正确性」保证的部分 —— 它是交互决策。
 * 所以抽成纯函数：六条规则每条都能单测，而不是散在 effect 里靠手点验证。
 *
 * 贯穿全部规则的一句话：**自动展开是便利，不是命令。** 一旦用户表达了自己的
 * 意图（关闭面板、切到别的产物、正在输入），便利就该让位。
 */

import type { ChatArtifact } from './artifacts'

/** 侧栏默认收起用的也是这个阈值 —— 更窄的窗口分栏会把会话挤成一条缝。 */
export const MIN_SPLIT_WIDTH = 1180

export interface AutoOpenState {
  /** 这一轮已经自动展开过了吗。 */
  openedThisTurn: boolean
  /** 用户在这一轮里主动关过面板吗 —— 明确的「我不想看」。 */
  dismissedThisTurn: boolean
  /** 用户当前正在看的产物 id（手动选过的）。 */
  viewing: string | null
  /** 窗口宽度。 */
  width: number
}

export type AutoOpenDecision =
  | { open: true; announce: string }
  | { open: false; reason: 'failed' | 'already-opened' | 'dismissed' | 'viewing-other' | 'too-narrow' }

/**
 * 该不该为这个产物自动展开面板。
 *
 * 顺序有意义：先排除「产物本身不值得开」（失败），再排除「用户已经表态」
 * （关过、正在看别的），最后才是环境限制（窗口太窄）。这样返回的 reason
 * 反映的是最主要的原因，而不是碰巧先判到的那条。
 */
export function decideAutoOpen (artifact: ChatArtifact, state: AutoOpenState): AutoOpenDecision {
  // 1. 失败的产物不开面板 —— 开一个空图比不开更糟。对话里会留失败卡片。
  if (artifact.status === 'failed') return { open: false, reason: 'failed' }

  // 2. 用户关过 = 明确的「我不想看」，本轮不再弹。
  if (state.dismissedThisTurn) return { open: false, reason: 'dismissed' }

  // 3. 正在看别的产物时不切走 —— 抢走用户正在读的东西是最烦的。
  //    新产物仍会进页签，页签条上提示有新增。
  if (state.viewing && state.viewing !== artifact.id) {
    return { open: false, reason: 'viewing-other' }
  }

  // 4. 每轮只自动展开第一个。annotate_chart 一轮可被调用多次（每只票一次），
  //    不限制的话连续三只票右侧跳三次。
  if (state.openedThisTurn) return { open: false, reason: 'already-opened' }

  // 5. 窄窗口不自动分栏。
  if (state.width < MIN_SPLIT_WIDTH) return { open: false, reason: 'too-narrow' }

  return { open: true, announce: artifact.title }
}

/** 新一轮开始：重置本轮的「已展开」与「用户关过」。 */
export function resetForTurn (state: AutoOpenState): AutoOpenState {
  return { ...state, openedThisTurn: false, dismissedThisTurn: false }
}
