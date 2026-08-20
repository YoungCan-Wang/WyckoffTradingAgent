/**
 * 设置行的基础组件。类名沿用原来的 CSS（.srow/.sleft/.slab/.seg…）——
 * 这次只换渲染方式，视觉必须一模一样，才能逐屏对比验证没搬坏。
 */
import { useEffect, useState, type ReactNode } from 'react'

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

/**
 * 分栏标题。data-sec-key 供 app.js 的 scrollToAnchor 定位 ——
 * 靠文本匹配会在英文界面失效，靠下标会随分栏增减而错位。
 */
export function SecHead ({ k }: { k: string }) {
  return <h3 className="sec" data-sec-key={k}>{window.WyckoffI18n.t(k)}</h3>
}

interface NumProps {
  value: number
  min: number
  max: number
  onCommit: (value: number) => void
  /** 超出范围时的提示，由调用方给译文。 */
  onRangeError: (min: number, max: number) => void
}

/**
 * 数字输入。本地先钳一次，后端不该看到越界值。
 *
 * 越界时回填原值而不是留着错的：留着的话用户以为已经生效了。
 */
export function Num ({ value, min, max, onCommit, onRangeError }: NumProps) {
  const [draft, setDraft] = useState(String(value))
  // prop 变了就同步草稿。
  //
  // 后端拒绝一个**在合法区间内**的值时，useSettings 会把 data 回滚，但输入框
  // 仍然显示那个被拒的数字 —— 用户看到的是「我改了，而且看起来生效了」。
  // 越界那条路径本来就回填了原值，漏的是这条。
  // （CashRow 里有同样的 effect，这里缺。）
  useEffect(() => { setDraft(String(value)) }, [value])
  return (
    <input
      className="num"
      type="number"
      min={min}
      max={max}
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => {
        const next = Number(draft)
        if (!Number.isFinite(next) || next < min || next > max) {
          setDraft(String(value))
          onRangeError(min, max)
          return
        }
        if (next !== value) onCommit(next)
      }}
    />
  )
}

/** 只读的「已配置 / 未配置」指示。密钥本身不出现在界面上。 */
export function KeyState ({ present }: { present: boolean }) {
  const t = window.WyckoffI18n.t
  return (
    <span className={present ? 'ok' : 'miss'}>
      {present ? t('datasource.configured') : t('datasource.notConfigured')}
    </span>
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
  step?: number
  /** 拖动中的即时预览，不落盘 */
  onPreview?: (value: number) => void
  onCommit: (value: number) => void
}

/**
 * 滑块。拖动时只预览不保存 —— 每一步都写盘会打出几十次 IPC 调用。
 * 受控值来自 props，本地 draft 负责拖动期间的显示。
 */
export function Range ({ value, min, max, step, onPreview, onCommit }: RangeProps) {
  const [draft, setDraft] = useState<number | null>(null)
  const shown = draft ?? value
  return (
    <input
      className="rng"
      type="range"
      min={min}
      max={max}
      step={step}
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
