/**
 * 读盘室 system / 当轮行情注入：保持 system 字节稳定，利于 prompt cache。
 */

export function buildStableChatSystemPrompt(parts: {
  rolePrompt: string
  webSearchGuidance?: string
}): string {
  return [parts.rolePrompt, parts.webSearchGuidance || ''].filter(Boolean).join('\n\n')
}

export function appendMarketWatchModelMessage<T extends { role: string }>(
  messages: T[],
  marketWatchContext: string,
): Array<T | { role: 'user'; content: string }> {
  const context = marketWatchContext.trim()
  if (!context) return messages
  return [...messages, { role: 'user', content: context }]
}
