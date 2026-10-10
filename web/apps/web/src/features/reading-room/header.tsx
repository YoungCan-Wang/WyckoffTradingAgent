import { Compass, MessageSquareText, Pin, type LucideIcon } from 'lucide-react'
import { usePreferences } from '@/lib/preferences'
import type { ChatConfig, ReadingRoomTab } from './types'

export function ChatHeader({
  config,
  hasUser,
  activeTab,
  messageCount,
  watchCount,
  onTabChange,
}: {
  config: ChatConfig
  hasUser: boolean
  activeTab: ReadingRoomTab
  messageCount: number
  watchCount: number
  onTabChange: (tab: ReadingRoomTab) => void
}) {
  const { t } = usePreferences()
  return (
    <div className="flex shrink-0 flex-wrap items-center justify-between gap-x-6 gap-y-3 border-b border-border px-6 py-3">
      <div className="flex min-w-0 items-center gap-3">
        <h1 className="text-lg font-semibold">{t('chat.title')}</h1>
        {config.model && <span className="rounded-full bg-indigo-50 px-2.5 py-0.5 text-[11px] text-indigo-700 dark:bg-indigo-500/10 dark:text-indigo-200">{config.model}</span>}
        {config.agentLane && <span title="会员 Agent 服务模式" className="rounded-full bg-emerald-50 px-2 py-0.5 text-[11px] text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-200">Agent</span>}
        {!config.configured && hasUser && <span className="rounded-full bg-amber-50 px-2 py-0.5 text-[11px] text-amber-700 dark:bg-amber-500/10 dark:text-amber-200">{config.error ? t('chat.configErrorBadge') : t('chat.noApiKey')}</span>}
      </div>
      <ReadingRoomTabs activeTab={activeTab} messageCount={messageCount} watchCount={watchCount} onChange={onTabChange} />
    </div>
  )
}

function ReadingRoomTabs({
  activeTab,
  messageCount,
  watchCount,
  onChange,
}: {
  activeTab: ReadingRoomTab
  messageCount: number
  watchCount: number
  onChange: (tab: ReadingRoomTab) => void
}) {
  const tabs: { key: ReadingRoomTab; label: string; count?: number; Icon: LucideIcon }[] = [
    { key: 'desk', label: '快捷入口', Icon: Compass },
    { key: 'chat', label: '对话', count: messageCount, Icon: MessageSquareText },
    { key: 'watchlist', label: '观察篮', count: watchCount, Icon: Pin },
  ]
  return (
    <div className="flex w-full max-w-md shrink-0 segmented-track" role="tablist" aria-label="读盘室子视图">
      {tabs.map(({ key, label, count, Icon }) => <TabButton key={key} tab={key} label={label} count={count} Icon={Icon} active={activeTab === key} onChange={onChange} />)}
    </div>
  )
}

function TabButton({
  tab,
  label,
  count,
  Icon,
  active,
  onChange,
}: {
  tab: ReadingRoomTab
  label: string
  count?: number
  Icon: LucideIcon
  active: boolean
  onChange: (tab: ReadingRoomTab) => void
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={() => onChange(tab)}
      className={`inline-flex h-8 flex-1 cursor-pointer items-center justify-center gap-1.5 whitespace-nowrap rounded-full px-3 text-xs font-bold transition-all duration-200 ${
        active ? 'bg-primary text-primary-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'
      }`}
    >
      <Icon size={13} />
      <span>{label}</span>
      {typeof count === 'number' && count > 0 && (
        <span className={`rounded-full px-1.5 py-0.5 text-[9px] font-bold transition-all ${active ? 'bg-primary-foreground/25 text-primary-foreground' : 'bg-muted text-muted-foreground'}`}>
          {count}
        </span>
      )}
    </button>
  )
}
