/**
 * 对话流里的提问卡。模型调 ask_user_question 时出现，这一轮停在这里等答复。
 *
 * 与确认卡的区别只在答复长什么样：这里是选项文本或自由输入，那里是 allow/deny。
 */
import { useState } from 'react'
import { collect } from '../lib/ipc'

const t = (key: string, params?: Record<string, string | number>) => window.WyckoffI18n.t(key, params)

interface Props {
  event: Record<string, unknown>
}

export function QuestionCardInline ({ event }: Props) {
  const [busy, setBusy] = useState(false)
  const [text, setText] = useState(String(event.default_answer || ''))
  const [outcome, setOutcome] = useState<{ kind: 'ok' | 'err'; text: string; retryable?: boolean } | null>(null)

  const questionId = String(event.question_id || '')
  const question = String(event.question || '')
  const options = Array.isArray(event.options) ? event.options.map(String) : []
  const allowFreeText = event.allow_free_text !== false

  const send = async (answer: string) => {
    if (!answer.trim()) return
    setBusy(true)
    const res = await collect('chat_answer', { question_id: questionId, answer }).catch(
      (err: unknown) => ({ __error: (err as Error)?.message || String(err) })
    )
    const errText = (res as { __error?: string } | null)?.__error
    if (errText) {
      setOutcome({ kind: 'err', text: errText, retryable: true })
      setBusy(false)
      return
    }
    // collect 失败返回 null。当成送到了会显示一个模型根本没收到的答复。
    if (!res) {
      setOutcome({ kind: 'err', text: t('confirm.callFailed'), retryable: true })
      setBusy(false)
      return
    }
    if (!(res as { delivered?: boolean }).delivered) {
      setOutcome({ kind: 'err', text: t('confirm.expired') })
      setBusy(false)
      return
    }
    setOutcome({ kind: 'ok', text: t('question.answered', { answer }) })
    setBusy(false)
  }

  return (
    <div className="card">
      <div className="r1"><b>{question || t('question.defaultPrompt')}</b></div>
      {outcome ? (
        <div className={outcome.kind === 'err' ? 'sys err' : 'sys'}>{outcome.text}</div>
      ) : null}
      {!outcome || outcome.retryable ? (
        <>
          {options.length ? (
            <div className="btns">
              {options.map((opt) => (
                <button type="button" key={opt} className="b" disabled={busy} onClick={() => send(opt)}>
                  {opt}
                </button>
              ))}
            </div>
          ) : null}
          {allowFreeText ? (
            <div className="btns">
              <input
                type="text"
                className="sf-input"
                value={text}
                disabled={busy}
                placeholder={t('question.placeholder')}
                onChange={(e) => setText(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') send(text) }}
              />
              <button type="button" className="b pri" disabled={busy || !text.trim()} onClick={() => send(text)}>
                {busy ? t('question.sending') : t('action.send')}
              </button>
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  )
}
