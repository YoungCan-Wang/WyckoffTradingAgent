/**
 * 会话产物注册表 + 自动展开的接线层。
 *
 * 决策本身在 autoOpen.ts（纯函数、可单测）；这里只负责把它接到真实世界：
 * 收事件、记状态、调 shell、宣告给读屏。
 *
 * 为什么单独一个 hook 而不是塞进 useChat：useChat 已经在管对话状态机
 * （流式分发、早到事件缓存、飞行计数）。产物是另一件事,混在一起会让两种
 * 失败模式互相干扰 —— 而且产物需要跨轮存活,对话轮不需要。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { mergeArtifact, parseArtifactEvent, type ChatArtifact } from './artifacts'
import { decideAutoOpen, resetForTurn, type AutoOpenState } from './autoOpen'

export interface ArtifactsApi {
  /** 本会话产生过的全部产物,按首次出现排序。 */
  artifacts: ChatArtifact[]
  /** 用户主动打开某个产物（对话卡片、页签点击都走这里）。 */
  open: (artifact: ChatArtifact) => void
  /** 新一轮开始 —— 重置「本轮已展开」与「本轮关过」。 */
  beginTurn: () => void
  /** 前端合成的产物（报告不走工具,由 done 时合成）。 */
  add: (artifact: ChatArtifact) => void
}

export function useArtifacts (): ArtifactsApi {
  const [artifacts, setArtifacts] = useState<ChatArtifact[]>([])
  // 自动展开状态用 ref：它在事件回调里读写,而回调的闭包会捕获旧 state。
  const auto = useRef<AutoOpenState>({
    openedThisTurn: false,
    dismissedThisTurn: false,
    viewing: null,
    width: typeof window === 'undefined' ? 1400 : window.innerWidth
  })

  /** 宣告给读屏,但**不移动焦点** —— 用户可能正在输入框里打字。 */
  const announce = useCallback((title: string) => {
    const node = document.getElementById('artifact-live')
    if (node) node.textContent = window.WyckoffI18n.t('artifact.opened', { title })
  }, [])

  const openNow = useCallback((artifact: ChatArtifact) => {
    auto.current.viewing = artifact.id
    window.WyckoffShell?.openArtifact?.(artifact)
  }, [])

  const open = useCallback((artifact: ChatArtifact) => {
    // 用户主动打开 = 他想看 —— 清掉「关过」,否则本轮后续产物永远不再自动展开。
    auto.current.dismissedThisTurn = false
    openNow(artifact)
  }, [openNow])

  const add = useCallback((artifact: ChatArtifact) => {
    setArtifacts((prev) => mergeArtifact(prev, artifact))
    auto.current.width = window.innerWidth
    const decision = decideAutoOpen(artifact, auto.current)
    if (!decision.open) return
    auto.current.openedThisTurn = true
    openNow(artifact)
    announce(decision.announce)
  }, [announce, openNow])

  const beginTurn = useCallback(() => {
    auto.current = resetForTurn(auto.current)
  }, [])

  // 后端产物事件。订阅一次,按 artifact_id 去重。
  useEffect(() => {
    const off = window.wyckoff.onEvent((event) => {
      const artifact = parseArtifactEvent(event as Record<string, unknown>)
      if (artifact) add(artifact)
    })
    return off
  }, [add])

  // 面板被关空 = 用户明确表示「我不想看」。本轮不再自动弹。
  //
  // 靠 count 归零而不是单个页签的 onClose：关掉其中一个页签只是换着看,
  // 全部关掉才是「收起面板」。
  useEffect(() => {
    const onCount = (event: Event) => {
      const count = Number((event as CustomEvent).detail?.count || 0)
      if (count === 0) {
        auto.current.dismissedThisTurn = true
        auto.current.viewing = null
      }
    }
    window.addEventListener('wyckoff:artifacts', onCount)
    return () => window.removeEventListener('wyckoff:artifacts', onCount)
  }, [])

  return { artifacts, open, beginTurn, add }
}
