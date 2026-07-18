import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type FormEvent,
  type RefObject,
  type SetStateAction,
} from 'react'
import {
  DefaultChatTransport,
  lastAssistantMessageIsCompleteWithApprovalResponses,
  type UIMessage,
} from 'ai'
import { useChat } from '@ai-sdk/react'
import { apiUrl } from '@/lib/api-url'
import type { TranslationKey } from '@/lib/preferences'
import type { ReadingRoomConversations } from './conversations'
import { scrollToMessage } from './run-records'
import type { ChatConfig, ChatRunEvent, ChatRunStatus, MarketWatchSnapshot, QueuedMessage, ReadingRoomTab, StageProgressStatus, WatchItem } from './types'
import { writeBooleanStorage } from './utils'

export const CONVERSATION_SIDEBAR_STORAGE_KEY = 'wyckoff:reading-room-sidebar-collapsed-v1'

const MAX_QUEUED_MESSAGES = 5

export type ReadingRoomChat = ReturnType<typeof useChat<UIMessage>>

export interface MessageQueue {
  messages: QueuedMessage[]
  enqueue: (text: string) => void
  clear: () => void
}

interface SubmitHandlerArgs {
  chat: ReadingRoomChat
  config: ChatConfig
  input: string
  loading: boolean
  queue: MessageQueue
  token: string | undefined
  t: (key: TranslationKey) => string
  setActiveTab: (value: ReadingRoomTab) => void
  setInput: Dispatch<SetStateAction<string>>
  setLocalError: Dispatch<SetStateAction<string>>
}

interface ReadingRoomActionArgs {
  chat: ReadingRoomChat
  config: ChatConfig
  conversations: ReadingRoomConversations
  loading: boolean
  queue: MessageQueue
  scrollRef: RefObject<HTMLDivElement | null>
  token: string | undefined
  t: (key: TranslationKey) => string
  setActiveTab: (value: ReadingRoomTab) => void
  setInput: Dispatch<SetStateAction<string>>
  setLocalError: Dispatch<SetStateAction<string>>
  setSidebarCollapsed: Dispatch<SetStateAction<boolean>>
}

export function useSubmitHandler(args: SubmitHandlerArgs) {
  const { chat, config, input, loading, queue, token, t, setActiveTab, setInput, setLocalError } = args
  const submitText = useCallback((rawText: string) => {
    const text = rawText.trim()
    if (!text) return
    if (!token) { setLocalError(t('chat.requestFailed')); return }
    if (!config.configured) { setLocalError(config.error || t('chat.configureLLM')); return }
    setActiveTab('chat')
    setInput('')
    setLocalError('')
    chat.clearError()
    if (loading) {
      queue.enqueue(text)
      return
    }
    void chat.sendMessage({ text })
  }, [chat, config.configured, config.error, loading, queue, setActiveTab, setInput, setLocalError, t, token])

  return useCallback((e: FormEvent) => {
    e.preventDefault()
    submitText(input)
  }, [input, submitText])
}

export function useReadingRoomActions(args: ReadingRoomActionArgs) {
  const {
    chat, config, conversations, loading, queue, scrollRef, token, t,
    setActiveTab, setInput, setLocalError, setSidebarCollapsed,
  } = args
  const resetInputState = useCallback(() => {
    queue.clear()
    setInput('')
    setLocalError('')
    setActiveTab('chat')
    chat.clearError()
  }, [chat, queue, setActiveTab, setInput, setLocalError])

  const selectConversation = useCallback((id: string) => {
    if (loading) void chat.stop()
    resetInputState()
    conversations.select(id)
  }, [chat, conversations, loading, resetInputState])

  const removeConversation = useCallback((id: string) => {
    if (loading) void chat.stop()
    resetInputState()
    conversations.remove(id)
  }, [chat, conversations, loading, resetInputState])

  const renameConversation = useCallback((id: string, title: string) => conversations.rename(id, title), [conversations])
  const toggleSidebar = useCallback(() => {
    setSidebarCollapsed((value) => {
      const next = !value
      writeBooleanStorage(CONVERSATION_SIDEBAR_STORAGE_KEY, next)
      return next
    })
  }, [setSidebarCollapsed])

  const openRunRecord = useCallback((messageId: string) => {
    setActiveTab('chat')
    window.setTimeout(() => scrollToMessage(scrollRef.current, messageId), 0)
  }, [scrollRef, setActiveTab])

  const startNewConversation = useStartNewConversation({
    chat, config, conversations, loading, queue, token, t, setActiveTab, setInput, setLocalError,
  })
  return { openRunRecord, removeConversation, renameConversation, selectConversation, startNewConversation, toggleSidebar }
}

function useStartNewConversation(args: Omit<ReadingRoomActionArgs, 'scrollRef' | 'setSidebarCollapsed'>) {
  const { chat, config, conversations, loading, queue, token, t, setActiveTab, setInput, setLocalError } = args
  return useCallback((rawText?: string) => {
    const text = typeof rawText === 'string' ? rawText.trim() : ''
    if (text && !token) { setLocalError(t('chat.requestFailed')); return }
    if (text && !config.configured) { setLocalError(config.error || t('chat.configureLLM')); return }
    if (loading) void chat.stop()
    queue.clear()
    setInput('')
    setLocalError('')
    setActiveTab('chat')
    conversations.create()
    chat.clearError()
    if (text) {
      window.setTimeout(() => {
        void chat.sendMessage({ text }).catch((error: unknown) => setLocalError(normalizeClientError(error, t)))
      }, 0)
    }
  }, [chat, config.configured, config.error, conversations, loading, queue, setActiveTab, setInput, setLocalError, t, token])
}

export function useReadingRoomChat(
  token: string | undefined,
  setLocalError: (value: string) => void,
  t: (key: TranslationKey) => string,
  setModelStatus: (value: ChatRunStatus | null) => void,
  watchlist: Pick<WatchItem, 'code' | 'name'>[],
  marketWatch: MarketWatchSnapshot | null,
  setMarketWatch: (value: MarketWatchSnapshot) => void,
  onRunEvent?: (event: ChatRunEvent) => void,
  onRunFinish?: () => void,
  onRunError?: () => void,
) {
  const watchlistRef = useRef(watchlist)
  const marketWatchRef = useRef(marketWatch)
  useEffect(() => { watchlistRef.current = watchlist }, [watchlist])
  useEffect(() => { marketWatchRef.current = marketWatch }, [marketWatch])
  const transport = useMemo(() => buildChatTransport(token, watchlistRef, marketWatchRef), [token])
  return useChat({
    transport,
    experimental_throttle: 50,
    sendAutomaticallyWhen: lastAssistantMessageIsCompleteWithApprovalResponses,
    onData: (part) => {
      if (part.type === 'data-run-event') onRunEvent?.(part.data as ChatRunEvent)
      if (part.type === 'data-model-status') setModelStatus(part.data as ChatRunStatus)
      if (part.type === 'data-market-watch') setMarketWatch(part.data as MarketWatchSnapshot)
      if (part.type === 'data-stage-progress') {
        const progress = part.data as StageProgressStatus
        setModelStatus(progress.state === 'completed' ? null : progress)
      }
    },
    onFinish: () => { setModelStatus(null); onRunFinish?.() },
    onError: (err) => { setModelStatus(null); onRunError?.(); setLocalError(err.message || t('chat.requestFailed')) },
  })
}

export function useChatConfig(
  token: string | undefined,
  t: (key: TranslationKey, vars?: Record<string, string>) => string,
): ChatConfig {
  const [config, setConfig] = useState<ChatConfig>({ configured: false, model: null })
  useEffect(() => {
    if (!token) return
    let cancelled = false
    fetchChatConfig(token, t)
      .then((next) => { if (!cancelled) setConfig(next) })
      .catch(() => {
        if (!cancelled) setConfig({ configured: false, model: null, error: t('chat.configUnreachable') })
      })
    return () => { cancelled = true }
  }, [t, token])
  return config
}

export function useMessageQueue(
  chat: ReadingRoomChat,
  loading: boolean,
  token: string | undefined,
  configured: boolean,
  setLocalError: (value: string) => void,
  t: (key: TranslationKey) => string,
): MessageQueue {
  const [messages, setMessages] = useState<QueuedMessage[]>([])
  const dispatchingRef = useRef('')
  const enqueue = useCallback((text: string) => {
    setMessages((items) => {
      if (items.length >= MAX_QUEUED_MESSAGES) {
        setLocalError(t('chat.queueFull'))
        return items
      }
      return [...items, { id: createQueuedMessageId(), text }]
    })
  }, [setLocalError, t])
  const clear = useCallback(() => setMessages([]), [])

  useEffect(() => {
    const next = messages[0]
    if (!next || loading || !token || !configured || dispatchingRef.current) return
    dispatchingRef.current = next.id
    setMessages((items) => items[0]?.id === next.id ? items.slice(1) : items.filter((item) => item.id !== next.id))
    setLocalError('')
    chat.clearError()
    void chat.sendMessage({ text: next.text })
      .catch((error: unknown) => setLocalError(normalizeClientError(error, t)))
      .finally(() => { dispatchingRef.current = '' })
  }, [chat, configured, loading, messages, setLocalError, t, token])

  return useMemo(() => ({ messages, enqueue, clear }), [clear, enqueue, messages])
}

export function useAutoScroll(
  ref: RefObject<HTMLDivElement | null>,
  messages: UIMessage[],
  loading: boolean,
  queuedCount: number,
) {
  useEffect(() => {
    ref.current?.scrollTo({ top: ref.current.scrollHeight, behavior: 'smooth' })
  }, [messages, loading, queuedCount, ref])
}

function createQueuedMessageId(): string {
  return `queued-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

function normalizeClientError(error: unknown, t: (key: TranslationKey) => string): string {
  return error instanceof Error ? error.message : t('chat.requestFailed')
}

function buildChatTransport(
  token: string | undefined,
  watchlistRef: RefObject<Pick<WatchItem, 'code' | 'name'>[]>,
  marketWatchRef: RefObject<MarketWatchSnapshot | null>,
) {
  return new DefaultChatTransport({
    api: apiUrl('/api/chat'),
    headers: (): Record<string, string> => token ? { Authorization: `Bearer ${token}` } : {},
    body: () => ({ watchlist: watchlistRef.current, marketWatch: marketWatchRef.current }),
  })
}

async function fetchChatConfig(
  token: string,
  t: (key: TranslationKey, vars?: Record<string, string>) => string,
): Promise<ChatConfig> {
  let response: Response
  try {
    response = await fetch(apiUrl('/api/chat/config'), {
      headers: { Authorization: `Bearer ${token}` },
    })
  } catch {
    return { configured: false, model: null, error: t('chat.configUnreachable') }
  }
  if (!response.ok) return { configured: false, model: null, error: t('chat.configHttpError', { status: String(response.status) }) }
  return await response.json() as ChatConfig
}
