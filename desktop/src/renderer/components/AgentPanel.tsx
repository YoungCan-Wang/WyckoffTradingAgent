/** 「智能体」页：语气 + 模型 + 超时 + 数据源 + 定时任务。 */
import { useState } from 'react'
import { Row, Num, KeyState, SecHead } from './Rows'
import { TonePanel } from './TonePanel'
import { ModelRow } from './ModelRow'
import { AddModelForm } from './AddModelForm'
import { DaemonSection } from './DaemonSection'
import { RemotePanel } from './RemotePanel'
import type { Settings } from '../types'

const t = (key: string, params?: Record<string, string | number>) => window.WyckoffI18n.t(key, params)

interface Props {
  data: Settings
  notes: Record<string, { text: string; error: boolean }>
  save: <K extends keyof Settings>(key: K, value: Settings[K]) => Promise<boolean>
  reload: () => Promise<void>
  onMessage: (text: string, isError?: boolean) => void
  flash: (key: string, text: string, error: boolean) => void
}

export function AgentPanel ({ data, notes, save, reload, onMessage, flash }: Props) {
  // 「其余模型」默认折叠：11 个模型全铺开要滚三屏，真正要紧的两行会被埋掉。
  const [showRest, setShowRest] = useState(false)

  const active = data.models.filter((m) => m.id === data.default_model || m.id === data.fallback_model)
  const rest = data.models.filter((m) => m.id !== data.default_model && m.id !== data.fallback_model)

  const row = (model: typeof data.models[number]) => (
    <ModelRow
      key={model.id}
      model={model}
      isDefault={model.id === data.default_model}
      isFallback={model.id === data.fallback_model}
      onChanged={reload}
    />
  )

  return (
    <>
      <TonePanel data={data} notes={notes} save={save} />

      <SecHead k="models.heading" />
      <p className="dlg-sub">{t('models.note')}</p>
      {active.map(row)}
      {rest.length ? (
        <div className="mmore">
          <button
            type="button"
            className={showRest ? 'mtoggle open' : 'mtoggle'}
            onClick={() => setShowRest(!showRest)}
          >
            {showRest ? t('models.othersCollapse') : t('models.others', { count: rest.length })}
          </button>
          {showRest ? <div>{rest.map(row)}</div> : null}
        </div>
      ) : null}
      <AddModelForm onAdded={reload} onMessage={onMessage} />

      <SecHead k="timeout.heading" />
      <p className="dlg-sub">{t('timeout.note')}</p>
      <Row
        label={t('timeout.stream')}
        note={notes.stream_chunk_timeout_seconds?.text}
        noteIsError={notes.stream_chunk_timeout_seconds?.error}
      >
        <Num
          value={data.stream_chunk_timeout_seconds}
          min={10}
          max={600}
          onCommit={(v) => void save('stream_chunk_timeout_seconds', v)}
          onRangeError={(min, max) =>
            flash('stream_chunk_timeout_seconds', t('common.range', { min, max }), true)}
        />
      </Row>
      <Row
        label={t('timeout.tool')}
        note={notes.tool_timeout_seconds?.text}
        noteIsError={notes.tool_timeout_seconds?.error}
      >
        <Num
          value={data.tool_timeout_seconds}
          min={5}
          max={300}
          onCommit={(v) => void save('tool_timeout_seconds', v)}
          onRangeError={(min, max) => flash('tool_timeout_seconds', t('common.range', { min, max }), true)}
        />
      </Row>

      <SecHead k="datasource.heading" />
      <p className="dlg-sub">{t('datasource.note')}</p>
      <Row label="TickFlow"><KeyState present={data.has_tickflow_key} /></Row>
      <Row label="Tushare"><KeyState present={data.has_tushare_token} /></Row>

      <DaemonSection onMessage={onMessage} />

      <RemotePanel />
    </>
  )
}
