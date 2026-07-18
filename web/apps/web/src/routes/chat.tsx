import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router'
import { useAuthStore } from '@/stores/auth'
import { usePreferences } from '@/lib/preferences'
import {
  CONVERSATION_SIDEBAR_STORAGE_KEY,
  useAutoScroll,
  useChatConfig,
  useMessageQueue,
  useReadingRoomActions,
  useReadingRoomChat,
  useSubmitHandler,
} from '@/features/reading-room/chat-state'
import { useReadingRoomConversations } from '@/features/reading-room/conversations'
import { ChatHeader } from '@/features/reading-room/header'
import { buildRunRecords } from '@/features/reading-room/run-records'
import { ChatMessages, ErrorBanner } from '@/features/reading-room/transcript'
import type { ChatRunEvent, ChatRunStatus, MarketWatchSnapshot, ReadingRoomTab, RunCheckpoint, WatchItem } from '@/features/reading-room/types'
import { readBooleanStorage } from '@/features/reading-room/utils'
import { appendRunEvent, clearRunCheckpoint, finishRun, readRunCheckpoint } from '@/features/reading-room/run-ledger'
import { readMarketWatchCache, writeMarketWatchCache } from '@/features/reading-room/market-watch-cache'
import { useReadingRoomWatchlist } from '@/features/reading-room/watchlist-state'

const ACTIVE_TAB_SESSION_KEY = 'wyckoff:reading-room-active-tab-v1'

function useRunLedger() {
  const activeConversationRef = useRef('')
  const [runCheckpoint, setRunCheckpoint] = useState<RunCheckpoint | null>(null)
  const onRunEvent = useCallback((event: ChatRunEvent) => {
    const conversationId = activeConversationRef.current
    if (conversationId) setRunCheckpoint(appendRunEvent(conversationId, event))
  }, [])
  const finish = useCallback((status: 'completed' | 'interrupted') => {
    const conversationId = activeConversationRef.current
    if (conversationId) setRunCheckpoint(finishRun(conversationId, status))
  }, [])
  const onRunFinish = useCallback(() => finish('completed'), [finish])
  const onRunError = useCallback(() => finish('interrupted'), [finish])
  return { activeConversationRef, runCheckpoint, setRunCheckpoint, onRunEvent, onRunFinish, onRunError }
}

function useMarketWatch(userId: string | undefined, items: WatchItem[]) {
  const [marketWatch, setMarketWatch] = useState<MarketWatchSnapshot | null>(null)
  const codes = useMemo(() => items.map((item) => item.code), [items])
  const requestItems = useMemo(() => items.map(({ code, name }) => ({ code, name })), [items])
  const updateMarketWatch = useCallback((snapshot: MarketWatchSnapshot) => {
    setMarketWatch(snapshot)
    writeMarketWatchCache(userId, snapshot)
  }, [userId])
  useEffect(() => setMarketWatch(readMarketWatchCache(userId, codes)), [userId, codes])
  return { marketWatch, requestItems, updateMarketWatch }
}

function useInitialPrompt(configured: boolean, token: string | undefined, start: (rawText?: string) => void) {
  const location = useLocation()
  const navigate = useNavigate()
  useEffect(() => {
    const prompt = (location.state as { initialPrompt?: unknown } | null)?.initialPrompt
    if (typeof prompt !== 'string' || !prompt.trim() || !token || !configured) return
    start(prompt)
    navigate(location.pathname, { replace: true, state: null })
  }, [configured, location.pathname, location.state, navigate, start, token])
}

export function ChatPage() {
  const session = useAuthStore((s) => s.session)
  const user = useAuthStore((s) => s.user)
  const { t } = usePreferences()
  const [input, setInput] = useState('')
  const [localError, setLocalError] = useState('')
  const [modelStatus, setModelStatus] = useState<ChatRunStatus | null>(null)
  const [activeTab, setActiveTab] = useState<ReadingRoomTab>(readActiveTab)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => readBooleanStorage(CONVERSATION_SIDEBAR_STORAGE_KEY, true))
  const scrollRef = useRef<HTMLDivElement>(null)
  const token = session?.access_token
  const config = useChatConfig(token, t)
  const { activeConversationRef, runCheckpoint, setRunCheckpoint, onRunEvent, onRunFinish, onRunError } = useRunLedger()
  const watchlist = useReadingRoomWatchlist(user?.id)
  const { marketWatch, requestItems, updateMarketWatch } = useMarketWatch(user?.id, watchlist.items)
  const chat = useReadingRoomChat(token, setLocalError, t, setModelStatus, requestItems, marketWatch, updateMarketWatch, onRunEvent, onRunFinish, onRunError)
  const loading = chat.status === 'submitted' || chat.status === 'streaming'
  const changeActiveTab = useCallback((tab: ReadingRoomTab) => { setActiveTab(tab); writeActiveTab(tab) }, [])
  const queue = useMessageQueue(chat, loading, token, config.configured, setLocalError, t)
  const conversations = useReadingRoomConversations(user?.id, chat.messages, chat.setMessages)
  useEffect(() => {
    activeConversationRef.current = conversations.activeId
    setRunCheckpoint(readRunCheckpoint(conversations.activeId))
  }, [conversations.activeId, activeConversationRef, setRunCheckpoint])
  const runRecords = useMemo(() => buildRunRecords(chat.messages, t), [chat.messages, t])
  useAutoScroll(scrollRef, activeTab === 'chat' ? chat.messages : [], activeTab === 'chat' && loading, activeTab === 'chat' ? queue.messages.length : 0)

  const handleSubmit = useSubmitHandler({
    chat,
    config,
    input,
    loading,
    queue,
    token,
    t,
    setActiveTab: changeActiveTab,
    setInput,
    setLocalError,
  })
  const actions = useReadingRoomActions({
    chat,
    config,
    conversations,
    loading,
    queue,
    scrollRef,
    token,
    t,
    setActiveTab: changeActiveTab,
    setInput,
    setLocalError,
    setSidebarCollapsed,
  })
  const { startNewConversation } = actions
  useInitialPrompt(config.configured, token, startNewConversation)
  const handleResumeRun = useCallback(() => {
    if (loading) return
    changeActiveTab('chat')
    setLocalError('')
    chat.clearError()
    void chat.sendMessage({ text: '请继续完成上一轮分析。复用当前对话中已经完成的工具结果，不要重复已完成的数据读取；如果上一轮缺少关键数据，只补充缺失步骤，然后给出最终结论。' })
  }, [changeActiveTab, chat, loading])
  const handleClearRunCheckpoint = useCallback(() => {
    if (activeConversationRef.current) {
      clearRunCheckpoint(activeConversationRef.current)
      setRunCheckpoint(null)
    }
  }, [activeConversationRef, setRunCheckpoint])

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden" data-reading-room-streaming={loading ? 'true' : 'false'}>
      <ChatHeader
        config={config}
        hasUser={Boolean(user)}
        activeTab={activeTab}
        messageCount={chat.messages.length}
        watchCount={watchlist.items.length}
        onTabChange={changeActiveTab}
      />
      <ChatMessages
        chat={chat}
        activeTab={activeTab}
        loading={loading}
        queuedMessages={queue.messages}
        conversations={conversations.items}
        activeConversationId={conversations.activeId}
        runRecords={runRecords}
        scrollRef={scrollRef}
        watchlist={watchlist.items}
        marketWatch={marketWatch}
        onOpenRecord={actions.openRunRecord}
        onNewConversation={actions.startNewConversation}
        sidebarCollapsed={sidebarCollapsed}
        onToggleSidebar={actions.toggleSidebar}
        onSelectConversation={actions.selectConversation}
        onRemoveConversation={actions.removeConversation}
        onRenameConversation={actions.renameConversation}
        onPinStock={watchlist.add}
        onRemoveWatchItem={watchlist.remove}
        onStart={actions.startNewConversation}
        input={input}
        queuedCount={queue.messages.length}
        onClearQueue={queue.clear}
        onInput={setInput}
        onSubmit={handleSubmit}
        onStop={() => { void chat.stop(); handleClearRunCheckpoint() }}
        modelStatus={modelStatus}
        runCheckpoint={runCheckpoint}
        onResumeRun={handleResumeRun}
        onClearRunCheckpoint={handleClearRunCheckpoint}
      />
      <ErrorBanner message={localError || chat.error?.message || ''} />
    </div>
  )
}

function readActiveTab(): ReadingRoomTab {
  if (typeof window === 'undefined') return 'desk'
  try {
    const value = window.sessionStorage.getItem(ACTIVE_TAB_SESSION_KEY)
    return value === 'chat' || value === 'watchlist' || value === 'desk' ? value : 'desk'
  } catch {
    return 'desk'
  }
}

function writeActiveTab(value: ReadingRoomTab): void {
  try {
    window.sessionStorage.setItem(ACTIVE_TAB_SESSION_KEY, value)
  } catch {
    // Session storage may be unavailable; the in-memory tab state still works.
  }
}
