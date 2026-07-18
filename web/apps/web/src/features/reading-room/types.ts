import type { MarketWatchQuote, MarketWatchSnapshot } from '@wyckoff/shared'

export interface WatchItem {
  id: string
  code: string
  name: string
  reason: string
  source: string
  trigger: string
  invalidation: string
  addedAt: string
  updatedAt: string
  score?: number | null
  changePct?: number | null
  phase?: string | null
  action?: string | null
}

export interface PinStockInput {
  code: string
  name?: string | null
  reason: string
  source: string
  trigger?: string | null
  invalidation?: string | null
  score?: number | null
  changePct?: number | null
  phase?: string | null
  action?: string | null
}

export interface ChatConfig {
  configured: boolean
  model: string | null
  error?: string
}

export interface QueuedMessage {
  id: string
  text: string
}

export type ReadingRoomTab = 'desk' | 'chat' | 'watchlist'

export interface ModelRunStatus {
  kind: 'model'
  phase: 'retrying' | 'fallback'
  model: string
  attempt: number
  nextModel?: string
}

export interface StageProgressStatus {
  kind: 'stage'
  stage: 'model'
  state: 'started' | 'completed'
  message?: string
  success?: boolean
  model: string
}

export type ChatRunStatus = ModelRunStatus | StageProgressStatus

export type { MarketWatchQuote, MarketWatchSnapshot }

export interface ChatRunEvent {
  runId: string
  sequence: number
  type: 'model_started' | 'model_completed' | 'model_failed' | 'tool_started' | 'tool_completed' | 'tool_failed' | 'answer_started' | string
  label: string
  toolName?: string
  toolCallId?: string
  timestamp: string
}

export interface RunCheckpoint {
  runId: string
  conversationId: string
  status: 'running' | 'completed' | 'interrupted'
  startedAt: string
  updatedAt: string
  events: ChatRunEvent[]
}

export interface RunRecord {
  id: string
  messageId: string
  title: string
  preview: string
  status: string
  toneClass: string
  toolLabels: string[]
}
