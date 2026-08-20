/**
 * 把一段 markdown 渲染进对话流。
 *
 * 复用 md.js 的 renderMarkdown 而不是在 React 里重写一遍：那份实现是逐节点
 * createElement/createTextNode 建出来的，**从不碰 innerHTML** —— 模型的输出
 * 可能带引用来的任意网页文本，这是那个文件的核心安全约束。重写一遍等于把这
 * 个约束重新赌一次。
 *
 * 代价是它返回 DOM 节点而不是 JSX，所以这里挂一个容器手动接管。
 */
import { useEffect, useRef } from 'react'

interface Props {
  source: string
  /** 标题下方的时间戳等；对话流里不需要。 */
  meta?: string
}

export function Markdown ({ source, meta }: Props) {
  const host = useRef<HTMLDivElement>(null)

  // 流式期间每来一个增量就重建一次。看着浪费，但这段文本最多几千字，
  // 而增量解析 markdown（半个 ** 算不算粗体？）会在边界上出错。
  useEffect(() => {
    const node = host.current
    if (!node) return
    // 挂 renderMarkdown 返回的那个节点本身（它自带 .doc）—— 整套排版样式
    // 直接继承，不用把十几条 .doc 规则复制一份给对话流。
    node.replaceChildren(window.WyckoffMd.renderMarkdown(source, meta))
  }, [source, meta])

  return <div className="md" ref={host} />
}
