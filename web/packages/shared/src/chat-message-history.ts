import { isToolUIPart, type UIMessage } from 'ai'

type ToolPartLocation = { messageIndex: number; partIndex: number }

function toolPartName(part: { type: string; toolName?: string }): string {
  if (part.type === 'dynamic-tool') return String(part.toolName || '')
  return part.type.startsWith('tool-') ? part.type.slice(5) : part.type
}

function summarizeProviderWebSearch(output: unknown): string {
  if (!output || typeof output !== 'object') return '此前曾用服务端联网搜索查阅公开信息。'
  const record = output as Record<string, unknown>
  const action = record.action && typeof record.action === 'object'
    ? record.action as Record<string, unknown>
    : null
  const query = typeof action?.query === 'string'
    ? action.query.trim()
    : Array.isArray(action?.queries)
      ? String(action.queries[0] || '').trim()
      : ''
  const sourceCount = Array.isArray(record.sources) ? record.sources.length : 0
  if (query && sourceCount > 0) return `此前联网搜索「${query}」并参考了 ${sourceCount} 个公开来源。`
  if (query) return `此前联网搜索「${query}」。`
  return '此前曾用服务端联网搜索查阅公开信息。'
}

/**
 * Chat Completions cannot round-trip provider-executed web_search parts:
 * they become dangling tool_calls without role:tool messages (HTTP 400).
 * Collapse them to short text so model/provider switches stay recoverable.
 */
export function sanitizeMessagesForChatTransport(messages: UIMessage[]): UIMessage[] {
  let changed = false
  const normalized = messages.flatMap((message) => {
    if (message.role !== 'assistant') return [message]
    const parts: UIMessage['parts'] = []
    let messageChanged = false
    for (const part of message.parts) {
      const isProviderWebSearch = isToolUIPart(part)
        && 'providerExecuted' in part
        && part.providerExecuted === true
        && toolPartName(part) === 'web_search'
      if (!isProviderWebSearch) {
        parts.push(part)
        continue
      }
      messageChanged = true
      changed = true
      if (part.state === 'output-available' && part.output != null) {
        parts.push({ type: 'text', text: summarizeProviderWebSearch(part.output) })
      }
    }
    if (parts.length === 0) {
      changed = true
      return []
    }
    return [messageChanged ? { ...message, parts } : message]
  })
  return changed ? normalized : messages
}

export function removeSupersededToolApprovals(messages: UIMessage[]): UIMessage[] {
  const outputs = new Map<string, ToolPartLocation>()
  messages.forEach((message, messageIndex) => {
    message.parts.forEach((part, partIndex) => {
      if (isToolUIPart(part) && (part.state === 'output-available' || part.state === 'output-error')) {
        outputs.set(part.toolCallId, { messageIndex, partIndex })
      }
    })
  })
  if (outputs.size === 0) return messages

  let changed = false
  const normalized = messages.flatMap((message, messageIndex) => {
    const parts = message.parts.filter((part, partIndex) => {
      if (!isToolUIPart(part) || part.state !== 'approval-responded') return true
      const output = outputs.get(part.toolCallId)
      if (!output) return true
      const superseded = messageIndex < output.messageIndex
        || (messageIndex === output.messageIndex && partIndex < output.partIndex)
      changed ||= superseded
      return !superseded
    })
    if (parts.length === 0) {
      changed = true
      return []
    }
    return [parts.length === message.parts.length ? message : { ...message, parts }]
  })
  return changed ? normalized : messages
}
