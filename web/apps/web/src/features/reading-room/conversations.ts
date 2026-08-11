import * as React from 'react'
import { useCallback, useEffect, useMemo, useRef } from 'react'
import type { UIMessage } from 'ai'
import { removeSupersededToolApprovals } from '@wyckoff/shared'
import { assistantText, isToolPart, messageText, type MessagePart } from './messages'
import { asRecord, sanitizeText } from './utils'

const CONVERSATION_LIMIT = 12
const CONVERSATION_MESSAGE_LIMIT = 80
const CONVERSATION_STORAGE_VERSION = 'reading-room-conversations-v1'

export interface ReadingRoomConversation {
  id: string
  title: string
  createdAt: string
  updatedAt: string
  messages: UIMessage[]
  titleEdited?: boolean
}

export interface ReadingRoomConversations {
  items: ReadingRoomConversation[]
  activeId: string
  create: () => void
  select: (id: string) => void
  remove: (id: string) => void
  rename: (id: string, title: string) => void
  replaceToolOutput: (conversationId: string, toolCallId: string, output: unknown) => void
}

type SetConversations = React.Dispatch<React.SetStateAction<ReadingRoomConversation[]>>
type SkipSaveRef = React.MutableRefObject<boolean>

export function useReadingRoomConversations(
  userId: string | undefined,
  messages: UIMessage[],
  setMessages: (messages: UIMessage[]) => void,
): ReadingRoomConversations {
  const storageKey = useMemo(() => conversationStorageKey(userId), [userId])
  const [items, setItems] = React.useState<ReadingRoomConversation[]>([])
  const [activeId, setActiveId] = React.useState('')
  const [loadedKey, setLoadedKey] = React.useState('')
  const skipNextSaveRef = useRef(false)

  useConversationLoader(storageKey, setItems, setActiveId, setMessages, setLoadedKey, skipNextSaveRef)
  useConversationSaver(storageKey, loadedKey, activeId, messages, setItems, skipNextSaveRef)
  const create = useCreateConversationAction(storageKey, setItems, setActiveId, setMessages, skipNextSaveRef)
  const select = useSelectConversationAction(items, setActiveId, setMessages, skipNextSaveRef)
  const remove = useRemoveConversationAction(storageKey, activeId, setItems, setActiveId, setMessages, skipNextSaveRef)
  const rename = useRenameConversationAction(storageKey, setItems)
  const replaceToolOutput = useReplaceToolOutput(storageKey, setItems)

  return useMemo(() => ({ items, activeId, create, select, remove, rename, replaceToolOutput }), [activeId, create, items, remove, rename, replaceToolOutput, select])
}

function useConversationLoader(
  storageKey: string,
  setItems: SetConversations,
  setActiveId: (id: string) => void,
  setMessages: (messages: UIMessage[]) => void,
  setLoadedKey: (key: string) => void,
  skipNextSaveRef: SkipSaveRef,
) {
  useEffect(() => {
    const loaded = readConversations(storageKey)
    const next = loaded.length > 0 ? loaded : [createConversation()]
    activateConversation(next[0] || createConversation(), setActiveId, setMessages, skipNextSaveRef)
    setItems(next)
    setLoadedKey(storageKey)
  }, [setActiveId, setItems, setLoadedKey, setMessages, skipNextSaveRef, storageKey])
}

function useConversationSaver(
  storageKey: string,
  loadedKey: string,
  activeId: string,
  messages: UIMessage[],
  setItems: SetConversations,
  skipNextSaveRef: SkipSaveRef,
) {
  useEffect(() => {
    if (loadedKey !== storageKey || !activeId) return
    if (skipNextSaveRef.current) {
      skipNextSaveRef.current = false
      return
    }
    setItems((current) => {
      const next = current.map((conversation) => {
        if (conversation.id !== activeId) return conversation
        // Live chat can lag behind replaceToolOutput after a mid-flight switch; keep
        // already-terminal sandbox outputs from the stored copy instead of clobbering them.
        const merged = preferStoredTerminalToolOutputs(messages, conversation.messages)
        return updateConversationMessages(conversation, merged)
      })
      writeConversations(storageKey, next)
      return next
    })
  }, [activeId, loadedKey, messages, setItems, skipNextSaveRef, storageKey])
}

function useCreateConversationAction(
  storageKey: string,
  setItems: SetConversations,
  setActiveId: (id: string) => void,
  setMessages: (messages: UIMessage[]) => void,
  skipNextSaveRef: SkipSaveRef,
) {
  return useCallback(() => {
    const conversation = createConversation()
    setItems((current) => {
      const next = [conversation, ...current].slice(0, CONVERSATION_LIMIT)
      writeConversations(storageKey, next)
      return next
    })
    activateConversation({ ...conversation, messages: [] }, setActiveId, setMessages, skipNextSaveRef)
  }, [setActiveId, setItems, setMessages, skipNextSaveRef, storageKey])
}

function useSelectConversationAction(
  items: ReadingRoomConversation[],
  setActiveId: (id: string) => void,
  setMessages: (messages: UIMessage[]) => void,
  skipNextSaveRef: SkipSaveRef,
) {
  return useCallback((id: string) => {
    const conversation = items.find((item) => item.id === id)
    if (conversation) activateConversation(conversation, setActiveId, setMessages, skipNextSaveRef)
  }, [items, setActiveId, setMessages, skipNextSaveRef])
}

function useRemoveConversationAction(
  storageKey: string,
  activeId: string,
  setItems: SetConversations,
  setActiveId: (id: string) => void,
  setMessages: (messages: UIMessage[]) => void,
  skipNextSaveRef: SkipSaveRef,
) {
  return useCallback((id: string) => {
    setItems((current) => {
      const { active, next } = removeConversation(current, id, activeId)
      activateConversation(active, setActiveId, setMessages, skipNextSaveRef)
      writeConversations(storageKey, next)
      return next
    })
  }, [activeId, setActiveId, setItems, setMessages, skipNextSaveRef, storageKey])
}

function useRenameConversationAction(storageKey: string, setItems: SetConversations) {
  return useCallback((id: string, title: string) => {
    const cleaned = normalizeConversationTitle(title)
    if (!cleaned) return
    setItems((current) => {
      const next = current.map((conversation) => renameConversation(conversation, id, cleaned))
      writeConversations(storageKey, next)
      return next
    })
  }, [setItems, storageKey])
}

function useReplaceToolOutput(storageKey: string, setItems: SetConversations) {
  return useCallback((conversationId: string, toolCallId: string, output: unknown) => {
    setItems((current) => {
      let changed = false
      const next = current.map((conversation) => {
        if (conversation.id !== conversationId) return conversation
        const messages = replaceConversationToolOutput(conversation.messages, toolCallId, output)
        if (messages === conversation.messages) return conversation
        changed = true
        return updateConversationMessages(conversation, messages)
      })
      if (changed) writeConversations(storageKey, next)
      return changed ? next : current
    })
  }, [setItems, storageKey])
}

function activateConversation(
  conversation: ReadingRoomConversation,
  setActiveId: (id: string) => void,
  setMessages: (messages: UIMessage[]) => void,
  skipNextSaveRef: SkipSaveRef,
) {
  setActiveId(conversation.id)
  skipNextSaveRef.current = true
  setMessages(conversation.messages)
}

function conversationStorageKey(userId: string | undefined): string {
  return `wyckoff:${userId || 'guest'}:${CONVERSATION_STORAGE_VERSION}`
}

function createConversation(): ReadingRoomConversation {
  const now = new Date().toISOString()
  return {
    id: `conversation-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    title: '新对话',
    createdAt: now,
    updatedAt: now,
    messages: [],
  }
}

function updateConversationMessages(conversation: ReadingRoomConversation, messages: UIMessage[]): ReadingRoomConversation {
  const savedMessages = messages.slice(-CONVERSATION_MESSAGE_LIMIT)
  const title = conversation.titleEdited ? conversation.title : conversationTitle(savedMessages, conversation.title)
  return {
    ...conversation,
    title,
    updatedAt: savedMessages.length > 0 ? new Date().toISOString() : conversation.updatedAt,
    messages: savedMessages,
  }
}

export function replaceConversationToolOutput(
  messages: UIMessage[],
  toolCallId: string,
  output: unknown,
): UIMessage[] {
  const normalized = removeSupersededToolApprovals(messages)
  const matches = normalized.flatMap((message, messageIndex) => message.role === 'assistant'
    ? message.parts.flatMap((part, partIndex) => {
      const tool = part as MessagePart
      return isToolPart(tool) && tool.toolCallId === toolCallId
        ? [{ messageIndex, partIndex, state: tool.state }]
        : []
    })
    : [])
  const canonical = matches.findLast((match) => match.state === 'output-available' || match.state === 'output-error')
  if (!canonical) return messages

  let changed = normalized !== messages
  const next = normalized.flatMap((message, messageIndex) => {
    if (message.role !== 'assistant') return message
    let messageChanged = false
    const parts = message.parts.flatMap((part, partIndex) => {
      const tool = part as MessagePart
      if (!isToolPart(tool) || tool.toolCallId !== toolCallId) return [part]
      if (messageIndex !== canonical.messageIndex || partIndex !== canonical.partIndex) return [part]
      if (tool.state === 'output-available' && tool.output === output) return [part]
      changed = true
      messageChanged = true
      return [{ ...tool, state: 'output-available' as const, output, errorText: undefined } as MessagePart]
    })
    messageChanged ||= parts.length !== message.parts.length
    if (parts.length === 0) return []
    return [messageChanged ? { ...message, parts } as UIMessage : message]
  })
  return changed ? next : messages
}

export function preferStoredTerminalToolOutputs(
  liveMessages: UIMessage[],
  storedMessages: UIMessage[],
): UIMessage[] {
  const terminalOutputs = terminalToolOutputs(storedMessages)
  if (terminalOutputs.size === 0) return liveMessages
  let changed = false
  const next = liveMessages.map((message) => {
    if (message.role !== 'assistant') return message
    let messageChanged = false
    const parts = message.parts.map((part) => {
      const tool = part as MessagePart
      if (!isToolPart(tool)) return part
      const storedOutput = terminalOutputs.get(tool.toolCallId)
      if (!storedOutput || isTerminalToolOutput(tool.output)) return part
      changed = true
      messageChanged = true
      return { ...tool, state: 'output-available' as const, output: storedOutput, errorText: undefined } as MessagePart
    })
    return messageChanged ? { ...message, parts } as UIMessage : message
  })
  return changed ? next : liveMessages
}

function terminalToolOutputs(messages: UIMessage[]): Map<string, unknown> {
  const outputs = new Map<string, unknown>()
  for (const message of messages) {
    if (message.role !== 'assistant') continue
    for (const part of message.parts as MessagePart[]) {
      if (!isToolPart(part) || !isTerminalToolOutput(part.output)) continue
      outputs.set(part.toolCallId, part.output)
    }
  }
  return outputs
}

function isTerminalToolOutput(output: unknown): boolean {
  const status = asRecord(output)?.status
  return status === 'completed' || status === 'failed' || status === 'cancelled'
}

function readConversations(key: string): ReadingRoomConversation[] {
  if (typeof window === 'undefined') return []
  try {
    const parsed = JSON.parse(window.localStorage.getItem(key) || '[]') as unknown
    if (!Array.isArray(parsed)) return []
    return parsed.map(normalizeConversation).filter(Boolean).slice(0, CONVERSATION_LIMIT) as ReadingRoomConversation[]
  } catch {
    return []
  }
}

function writeConversations(key: string, conversations: ReadingRoomConversation[]) {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(key, JSON.stringify(conversations.slice(0, CONVERSATION_LIMIT)))
  } catch {
    // localStorage may be disabled or full; the active conversation still works in memory.
  }
}

function normalizeConversation(value: unknown): ReadingRoomConversation | null {
  const item = asRecord(value)
  if (!item) return null
  const id = sanitizeText(item.id)
  if (!id) return null
  const messages = Array.isArray(item.messages) ? (item.messages as UIMessage[]).slice(-CONVERSATION_MESSAGE_LIMIT) : []
  const createdAt = sanitizeText(item.createdAt) || new Date().toISOString()
  return {
    id,
    title: sanitizeText(item.title) || conversationTitle(messages, '读盘对话'),
    createdAt,
    updatedAt: sanitizeText(item.updatedAt) || createdAt,
    messages,
    titleEdited: Boolean(item.titleEdited),
  }
}

function conversationTitle(messages: UIMessage[], fallback: string): string {
  const firstUser = messages.find((message) => message.role === 'user')
  if (!firstUser) return fallback || '新对话'
  if (!firstTurnHasAssistant(messages, firstUser.id)) return fallback || '新对话'
  return autoConversationTitle(messageText(firstUser), fallback)
}

function firstTurnHasAssistant(messages: UIMessage[], firstUserId: string): boolean {
  const userIndex = messages.findIndex((message) => message.id === firstUserId)
  if (userIndex < 0) return false
  const assistant = messages.slice(userIndex + 1).find((message) => message.role === 'assistant')
  if (!assistant) return false
  if (assistantText(assistant)) return true
  return (assistant.parts as MessagePart[]).some((part) => isToolPart(part) && part.state === 'output-available')
}

function autoConversationTitle(text: string, fallback: string): string {
  const source = normalizeConversationTitle(text)
  if (!source) return fallback || '读盘对话'
  const codeMatch = source.match(/\b(?:\d{6}|[A-Z]{2,5})\b/)
  if (/盘前/.test(source)) return '盘前读盘'
  if (/盘中|临场/.test(source)) return '盘中判断'
  if (/尾盘/.test(source)) return '尾盘机会'
  if (/收盘|复盘/.test(source)) return '收盘复盘'
  if (/市场水温|市场先验|大盘|指数|风险级别/.test(source)) return '市场水温'
  if (/持仓|仓位|止损|成本/.test(source)) return '持仓风险'
  if (/漏斗|选股|候选/.test(source)) return '候选漏斗'
  if (/策略归因|归因|信号/.test(source)) return '策略归因'
  if (/观察篮/.test(source)) return '观察篮复盘'
  if (/研报|深度报告/.test(source)) return codeMatch ? `${codeMatch[0]} 研报` : '个股研报'
  if (/诊断|分析|读一下/.test(source) && codeMatch) return `${codeMatch[0]} 个股诊断`
  return truncateTitle(stripTitleLeadWords(source), 18) || fallback || '读盘对话'
}

function stripTitleLeadWords(value: string): string {
  return value
    .replace(/^(帮我|请|麻烦|做一次|先|给我|我想|重点|读取|查看|运行)+/g, '')
    .replace(/[，。！？；：,.!?;:].*$/, '')
    .trim()
}

export function normalizeConversationTitle(value: string): string {
  return value
    .replace(/\s+/g, ' ')
    .replace(/[｜|]+/g, ' ')
    .trim()
    .slice(0, 40)
}

export function formatConversationDate(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '刚刚'
  const diffMs = Date.now() - date.getTime()
  if (diffMs < 60_000) return '刚刚'
  if (diffMs < 3_600_000) return `${Math.max(1, Math.floor(diffMs / 60_000))} 分钟前`
  const now = new Date()
  const sameDay = date.getFullYear() === now.getFullYear() && date.getMonth() === now.getMonth() && date.getDate() === now.getDate()
  const time = `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
  return sameDay ? `今天 ${time}` : `${date.getMonth() + 1}/${date.getDate()} ${time}`
}

function removeConversation(current: ReadingRoomConversation[], id: string, activeId: string) {
  const remaining = current.filter((item) => item.id !== id)
  const next = remaining.length > 0 ? remaining : [createConversation()]
  const active = (id === activeId ? next[0] : next.find((item) => item.id === activeId) || next[0]) || createConversation()
  return { next, active }
}

function renameConversation(conversation: ReadingRoomConversation, id: string, title: string): ReadingRoomConversation {
  if (conversation.id !== id) return conversation
  return { ...conversation, title, titleEdited: true, updatedAt: new Date().toISOString() }
}

function truncateTitle(value: string, maxLength: number): string {
  const text = value.trim()
  if (text.length <= maxLength) return text
  return `${text.slice(0, maxLength - 1)}...`
}
