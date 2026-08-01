import { isToolUIPart, type UIMessage } from 'ai'

type ToolPartLocation = { messageIndex: number; partIndex: number }

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
