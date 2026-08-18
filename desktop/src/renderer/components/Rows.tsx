/**
 * 设置行的基础组件。类名沿用原来的 CSS（.srow/.sleft/.slab/.seg…）——
 * 这次只换渲染方式，视觉必须一模一样，才能逐屏对比验证没搬坏。
 */
import { useState, type ReactNode } from 'react'

interface RowProps {
  label: string
  hint?: string
  children: ReactNode
  /** 保存反馈：'saved' | 'failed' | null，显示在标签下方 */
  note?: string | null
  noteIsError?: boolean
}

export function Row ({ label, hint, children, note, noteIsError }: RowProps) {
  return (
    <div className="srow">
      <div className="sleft">
        <span className="slab">{label}</span>
        {hint ? <span className="shint">{hint}</span> : null}
      </div>
      {children}
      {/* .snote 是 flex:none，设计成 .srow 的直接子元素（控件右侧），
          放进 .sleft 会变成纵向堆叠、位置不对。与 vanilla 版一致。 */}
      {note ? <span className={noteIsError ? 'snote err' : 'snote'}>{note}</span> : null}
    </div>
  )
}

interface SegProps<T> {
  value: T
  choices: Array<[T, string]>
  onPick: (value: T) => void
}

/** 分段控件。乐观切换：先动高亮，保存失败由调用方给出提示。 */
export function Seg<T extends string | number | boolean> ({ value, choices, onPick }: SegProps<T>) {
  return (
    <div className="seg">
      {choices.map(([choice, text]) => (
        <button
          key={String(choice)}
          type="button"
          className={choice === value ? 'seg-b on' : 'seg-b'}
          onClick={() => onPick(choice)}
        >
          {text}
        </button>
      ))}
    </div>
  )
}

interface RangeProps {
  value: number
  min: number
  max: number
  /** 拖动中的即时预览，不落盘 */
  onPreview?: (value: number) => void
  onCommit: (value: number) => void
}

/**
 * 滑块。拖动时只预览不保存 —— 每一步都写盘会打出几十次 IPC 调用。
 * 受控值来自 props，本地 draft 负责拖动期间的显示。
 */
export function Range ({ value, min, max, onPreview, onCommit }: RangeProps) {
  const [draft, setDraft] = useState<number | null>(null)
  const shown = draft ?? value
  return (
    <input
      className="rng"
      type="range"
      min={min}
      max={max}
      value={shown}
      onChange={(e) => {
        const next = Number(e.target.value)
        setDraft(next)
        onPreview?.(next)
      }}
      onMouseUp={() => {
        if (draft !== null) onCommit(draft)
        setDraft(null)
      }}
      onKeyUp={() => {
        if (draft !== null) onCommit(draft)
        setDraft(null)
      }}
    />
  )
}
