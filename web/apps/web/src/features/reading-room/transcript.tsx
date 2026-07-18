import { type FormEvent, type RefObject } from 'react'
import { AlertTriangle, LoaderCircle, MessageSquareText, RotateCcw, X } from 'lucide-react'
import type { UIMessage } from 'ai'
import { ChatComposer, ThinkingBubble } from './composer'
import { ConversationSidebar } from './conversation-sidebar'
import type { ReadingRoomConversation } from './conversations'
import { ReadingRoomDashboard } from './dashboard'
import type { ReadingRoomChat } from './chat-state'
import { MessageBubble, QueuedMessageBubble } from './tool-rendering'
import type { ChatRunStatus, MarketWatchSnapshot, PinStockInput, QueuedMessage, ReadingRoomTab, RunCheckpoint, RunRecord, WatchItem } from './types'
import { WatchlistPanelView } from './watchlist'

interface ChatMessagesProps {
  chat: ReadingRoomChat
  activeTab: ReadingRoomTab
  loading: boolean
  queuedMessages: QueuedMessage[]
  conversations: ReadingRoomConversation[]
  activeConversationId: string
  runRecords: RunRecord[]
  scrollRef: RefObject<HTMLDivElement | null>
  watchlist: WatchItem[]
  marketWatch: MarketWatchSnapshot | null
  onOpenRecord: (messageId: string) => void
  onNewConversation: () => void
  sidebarCollapsed: boolean
  onToggleSidebar: () => void
  onSelectConversation: (id: string) => void
  onRemoveConversation: (id: string) => void
  onRenameConversation: (id: string, title: string) => void
  onPinStock: (item: PinStockInput) => void
  onRemoveWatchItem: (code: string) => void
  onStart: (value: string) => void
  input: string
  queuedCount: number
  onClearQueue: () => void
  onInput: (value: string) => void
  onSubmit: (e: FormEvent) => void
  onStop: () => void
  modelStatus: ChatRunStatus | null
  runCheckpoint: RunCheckpoint | null
  onResumeRun: () => void
  onClearRunCheckpoint: () => void
}

export function ChatMessages(props: ChatMessagesProps) {
  const activeAssistantId = props.loading ? lastAssistantId(props.chat.messages) : null
  return (
    <div className="min-h-0 flex-1 overflow-hidden">
      <div className="flex h-full w-full flex-col lg:flex-row">
        <ChatConversationSidebar props={props} />
        <div className="flex min-w-0 flex-1 flex-col">
          <div ref={props.scrollRef} className="min-h-0 flex-1 overflow-auto px-4 py-5 sm:px-6">
            <ReadingRoomMainContent
              activeTab={props.activeTab}
              activeAssistantId={activeAssistantId}
              chat={props.chat}
              loading={props.loading}
              queuedMessages={props.queuedMessages}
              runRecords={props.runRecords}
              watchlist={props.watchlist}
              marketWatch={props.marketWatch}
              onOpenRecord={props.onOpenRecord}
              onPinStock={props.onPinStock}
              onRemoveWatchItem={props.onRemoveWatchItem}
              onStart={props.onStart}
              modelStatus={props.modelStatus}
              runCheckpoint={props.runCheckpoint}
              onResumeRun={props.onResumeRun}
              onClearRunCheckpoint={props.onClearRunCheckpoint}
            />
          </div>
          <ChatComposerSlot props={props} />
        </div>
      </div>
    </div>
  )
}

function ChatConversationSidebar({ props }: { props: ChatMessagesProps }) {
  if (props.activeTab !== 'chat') return null
  return (
    <ConversationSidebar
      conversations={props.conversations}
      activeId={props.activeConversationId}
      collapsed={props.sidebarCollapsed}
      onCreate={props.onNewConversation}
      onToggle={props.onToggleSidebar}
      onSelect={props.onSelectConversation}
      onRemove={props.onRemoveConversation}
      onRename={props.onRenameConversation}
    />
  )
}

function ChatComposerSlot({ props }: { props: ChatMessagesProps }) {
  if (props.activeTab !== 'chat') return null
  return (
    <ChatComposer
      input={props.input}
      loading={props.loading}
      queuedCount={props.queuedCount}
      onClearQueue={props.onClearQueue}
      onInput={props.onInput}
      onSubmit={props.onSubmit}
      onStop={props.onStop}
    />
  )
}

function ReadingRoomMainContent({
  activeTab,
  activeAssistantId,
  chat,
  loading,
  queuedMessages,
  runRecords,
  watchlist,
  marketWatch,
  onOpenRecord,
  onPinStock,
  onRemoveWatchItem,
  onStart,
  modelStatus,
  runCheckpoint,
  onResumeRun,
  onClearRunCheckpoint,
}: Pick<ChatMessagesProps, 'activeTab' | 'chat' | 'loading' | 'queuedMessages' | 'runRecords' | 'watchlist' | 'marketWatch' | 'onOpenRecord' | 'onPinStock' | 'onRemoveWatchItem' | 'onStart'> & {
  activeAssistantId: string | null
  modelStatus: ChatRunStatus | null
  runCheckpoint: RunCheckpoint | null
  onResumeRun: () => void
  onClearRunCheckpoint: () => void
}) {
  if (activeTab === 'desk') {
    return (
      <div className="mx-auto w-full max-w-5xl px-2 py-1 animate-fade-in-up">
        <ReadingRoomDashboard runRecords={runRecords} onOpenRecord={onOpenRecord} onStart={onStart} />
      </div>
    )
  }
  if (activeTab === 'watchlist') {
    return (
      <div className="mx-auto w-full max-w-5xl px-2 py-1 animate-fade-in-up">
        <WatchlistPanelView watchlist={watchlist} marketWatch={marketWatch} onRemove={onRemoveWatchItem} onStart={onStart} />
      </div>
    )
  }
  return (
    <div className="mx-auto w-full max-w-5xl">
      <ChatTranscript
        activeAssistantId={activeAssistantId}
        chat={chat}
        loading={loading}
        queuedMessages={queuedMessages}
        onPinStock={onPinStock}
        modelStatus={modelStatus}
        runCheckpoint={runCheckpoint}
        onResumeRun={onResumeRun}
        onClearRunCheckpoint={onClearRunCheckpoint}
      />
    </div>
  )
}

function ChatTranscript({
  activeAssistantId,
  chat,
  loading,
  queuedMessages,
  onPinStock,
  modelStatus,
  runCheckpoint,
  onResumeRun,
  onClearRunCheckpoint,
}: Pick<ChatMessagesProps, 'chat' | 'loading' | 'queuedMessages' | 'onPinStock'> & { activeAssistantId: string | null; modelStatus: ChatRunStatus | null; runCheckpoint: RunCheckpoint | null; onResumeRun: () => void; onClearRunCheckpoint: () => void }) {
  if (chat.messages.length === 0 && !loading && queuedMessages.length === 0 && !runCheckpoint) return <EmptyChatPanel />
  return (
    <div className="space-y-5 pb-4 animate-fade-in-up">
      {modelStatus && <ModelStatusBanner status={modelStatus} />}
      {!loading && runCheckpoint && runCheckpoint.status !== 'completed' && (
        <RunRecoveryBanner checkpoint={runCheckpoint} onResume={onResumeRun} onClear={onClearRunCheckpoint} />
      )}
      {chat.messages.map((message) => (
        <MessageBubble
          key={message.id}
          message={message}
          isActive={message.id === activeAssistantId}
          approve={(id) => void chat.addToolApprovalResponse({ id, approved: true })}
          deny={(id) => void chat.addToolApprovalResponse({ id, approved: false })}
          onPinStock={onPinStock}
        />
      ))}
      {queuedMessages.map((message, index) => <QueuedMessageBubble key={message.id} message={message} index={index + 1} />)}
      {loading && <ThinkingBubble />}
    </div>
  )
}

function RunRecoveryBanner({ checkpoint, onResume, onClear }: { checkpoint: RunCheckpoint; onResume: () => void; onClear: () => void }) {
  const lastEvent = checkpoint.events.at(-1)
  return (
    <div className="rounded-lg border border-sky-200 bg-sky-50 px-3 py-3 text-xs text-sky-900 dark:border-sky-500/30 dark:bg-sky-500/10 dark:text-sky-100">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="font-medium">上一轮分析可能在中途断开</div>
          <div className="mt-1 text-sky-800/80 dark:text-sky-100/80">已记录 {checkpoint.events.length} 个执行事件{lastEvent ? `，停在：${lastEvent.label}` : ''}。继续时会复用当前对话，尽量避免重复已完成的数据读取。</div>
        </div>
        <button type="button" onClick={onClear} aria-label="关闭恢复提示" className="shrink-0 rounded p-1 text-sky-700 hover:bg-sky-100 dark:text-sky-200 dark:hover:bg-sky-500/20"><X size={14} /></button>
      </div>
      <button type="button" onClick={onResume} className="mt-2 inline-flex items-center gap-1.5 rounded-md bg-sky-700 px-2.5 py-1.5 font-medium text-white hover:bg-sky-800 dark:bg-sky-600 dark:hover:bg-sky-500"><RotateCcw size={13} />继续本轮</button>
    </div>
  )
}

function ModelStatusBanner({ status }: { status: ChatRunStatus }) {
  if (status.kind === 'stage') {
    return (
      <div className="flex items-center gap-2 border-l-2 border-primary px-3 py-1.5 text-xs text-muted-foreground" role="status" aria-live="polite">
        <LoaderCircle size={14} className="shrink-0 animate-spin text-primary" />
        <span className="font-medium text-foreground">{status.message || '正在分析'}</span>
        <span className="h-1 w-1 shrink-0 rounded-full bg-border" aria-hidden="true" />
        <span className="min-w-0 truncate">{status.model}</span>
        <span className="ml-auto shrink-0 text-[11px] text-muted-foreground/70">执行中</span>
      </div>
    )
  }

  const label = status.phase === 'fallback'
    ? `当前模型 ${status.model} 暂时不可用，正在切换到 ${status.nextModel || '备用模型'}`
    : `当前模型响应异常，正在重试（第 ${status.attempt} 次）`
  return (
    <div className="flex items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-100" role="status" aria-live="polite">
      <AlertTriangle size={14} className="shrink-0" />
      <span>{label}</span>
    </div>
  )
}

function lastAssistantId(messages: UIMessage[]): string | null {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index]
    if (message?.role === 'assistant') return message.id
  }
  return null
}

function EmptyChatPanel() {
  return (
    <div className="mx-auto flex min-h-[360px] w-full max-w-4xl flex-col items-center justify-center rounded-lg border border-dashed border-border/75 bg-background px-6 text-center">
      <div className="rounded-full bg-muted p-3 text-muted-foreground">
        <MessageSquareText size={22} />
      </div>
      <p className="mt-3 text-sm font-medium">对话还没有开始</p>
      <p className="mt-1 max-w-md text-xs leading-5 text-muted-foreground">在下方输入你的读盘问题，执行过程和结论会保存在当前对话里。</p>
    </div>
  )
}

export function ErrorBanner({ message }: { message: string }) {
  if (!message) return null
  return <div className="mx-6 mb-2 shrink-0 rounded-lg bg-red-50 px-3 py-2 text-xs text-red-700 dark:bg-red-500/10 dark:text-red-200">{message}</div>
}
