import type { UIMessage } from 'ai'
import type { TranslationKey } from '@/lib/preferences'
import { assistantText, isToolPart, messageText, type MessagePart } from './messages'
import { formatToolName, getToolName, isRunningTool } from './tool-rendering-model'
import type { RunRecord } from './types'

export function buildRunRecords(messages: UIMessage[], t: (key: TranslationKey) => string): RunRecord[] {
  const records: RunRecord[] = []
  for (let index = 0; index < messages.length; index += 1) {
    const message = messages[index]
    if (!message || message.role !== 'user') continue
    const text = messageText(message)
    if (text.includes('请继续完成') || text.includes('继续完成当前分析')) continue
    const relatedAssistant = findRelatedAssistant(messages, index)
    const assistantParts = relatedAssistant?.parts || []
    const status = runRecordStatus(relatedAssistant)
    records.push({
      id: `run-${message.id}`,
      messageId: message.id,
      title: truncateText(messageText(message), 92),
      preview: truncateText(assistantText(relatedAssistant), 180),
      status: status.label,
      toneClass: status.toneClass,
      toolLabels: uniqueRunToolLabels(assistantParts, t),
    })
  }
  return records.reverse()
}

export function scrollToMessage(container: HTMLDivElement | null, messageId: string) {
  if (!container) return
  const target = Array.from(container.querySelectorAll<HTMLElement>('[data-message-id]'))
    .find((element) => element.dataset.messageId === messageId)
  target?.scrollIntoView({ block: 'center', behavior: 'smooth' })
}

function findRelatedAssistant(messages: UIMessage[], userIndex: number): UIMessage | null {
  for (let index = userIndex + 1; index < messages.length; index += 1) {
    const message = messages[index]
    if (!message) continue
    if (message.role === 'user') return null
    if (message.role === 'assistant') return message
  }
  return null
}

function runRecordStatus(message: UIMessage | null): { label: string; toneClass: string } {
  if (!message) return { label: '等待中', toneClass: 'bg-muted text-muted-foreground' }
  const parts = message.parts as MessagePart[]
  if (parts.some((part) => isToolPart(part) && isRunningTool(part))) return { label: '运行中', toneClass: 'bg-sky-50 text-sky-700 dark:bg-sky-500/10 dark:text-sky-200' }
  if (parts.some((part) => isToolPart(part) && part.state === 'output-error')) return { label: '部分失败', toneClass: 'bg-amber-50 text-amber-700 dark:bg-amber-500/10 dark:text-amber-200' }
  return { label: '完成', toneClass: 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-200' }
}

function uniqueRunToolLabels(parts: UIMessage['parts'], t: (key: TranslationKey) => string): string[] {
  const labels: string[] = []
  for (const part of parts as MessagePart[]) {
    if (!isToolPart(part)) continue
    const label = formatToolName(getToolName(part), t)
    if (!labels.includes(label)) labels.push(label)
  }
  return labels.slice(0, 4)
}

function truncateText(value: string, maxLength: number): string {
  const text = value.trim()
  if (text.length <= maxLength) return text
  return `${text.slice(0, maxLength - 1)}...`
}
