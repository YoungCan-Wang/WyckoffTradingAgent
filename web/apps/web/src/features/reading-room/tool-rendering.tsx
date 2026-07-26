import { memo } from 'react'
import { Check, LoaderCircle, Wrench, X } from 'lucide-react'
import type { UIMessage } from 'ai'
import { MarkdownContent } from '@/components/markdown'
import { usePreferences } from '@/lib/preferences'
import { parseAgentRunRecord, type AgentRunRecord } from './agent-runs'
import { type MessagePart, type ToolPart } from './messages'
import type { PinStockInput } from './types'
import { ToolStructuredOutput } from './tool-structured-cards'
import {
  STRUCTURED_TOOL_NAMES,
  buildAssistantRenderItems,
  formatToolName,
  getToolName,
  isRunningTool,
  toolChipLabel,
  toolGroupState,
  toolGroupStateTone,
  toolGroupTitle,
  toolProgressDescription,
  toolResultDigest,
  toolStateLabel,
  toolStepMarkerClass,
  toolStepStateTone,
  toolToneClass,
} from './tool-rendering-model'

export const MessageBubble = memo(function MessageBubble({
  message,
  isActive,
  approve,
  deny,
  onPinStock,
  agentRunRecords,
  onCancelAgentRun,
  onInterpretAgentRun,
}: {
  message: UIMessage
  isActive: boolean
  approve: (approvalId: string) => void
  deny: (approvalId: string) => void
  onPinStock: (item: PinStockInput) => void
  agentRunRecords: Record<string, AgentRunRecord>
  onCancelAgentRun: (runId: string) => Promise<void>
  onInterpretAgentRun: (runId: string) => void
}) {
  const isUser = message.role === 'user'
  return (
    <div data-message-id={message.id} className={`scroll-mt-4 flex ${isUser ? 'justify-end' : 'justify-start'}`}>
      <div className={isUser ? 'max-w-[78%] rounded-2xl bg-primary px-4 py-2.5 text-sm text-primary-foreground whitespace-pre-wrap' : 'w-full text-sm leading-7 text-foreground'}>
        {isUser ? <UserText message={message} /> : <AssistantParts message={message} isActive={isActive} approve={approve} deny={deny} onPinStock={onPinStock} agentRunRecords={agentRunRecords} onCancelAgentRun={onCancelAgentRun} onInterpretAgentRun={onInterpretAgentRun} />}
      </div>
    </div>
  )
})

export function QueuedMessageBubble({ message, index }: { message: { text: string }; index: number }) {
  const { t } = usePreferences()
  return (
    <div className="flex justify-end">
      <div className="max-w-[78%] rounded-2xl bg-primary/80 px-4 py-2.5 text-sm text-primary-foreground">
        <div className="whitespace-pre-wrap">{message.text}</div>
        <div className="mt-1 text-right text-[10px] opacity-80">
          {t('chat.queued').replace('{index}', String(index))}
        </div>
      </div>
    </div>
  )
}

function AssistantParts({
  message,
  isActive,
  approve,
  deny,
  onPinStock,
  agentRunRecords,
  onCancelAgentRun,
  onInterpretAgentRun,
}: {
  message: UIMessage
  isActive: boolean
  approve: (id: string) => void
  deny: (id: string) => void
  onPinStock: (item: PinStockInput) => void
  agentRunRecords: Record<string, AgentRunRecord>
  onCancelAgentRun: (runId: string) => Promise<void>
  onInterpretAgentRun: (runId: string) => void
}) {
  const items = buildAssistantRenderItems(message.parts as MessagePart[])
  return (
    <>
      {items.map((item) => {
        if (item.type === 'text') return <MarkdownContent key={item.key} content={item.content} />
        if (item.type === 'tool-group') return <ToolRunSummary key={item.key} parts={item.parts} isActive={isActive} />
        if (item.type === 'tool') return <ToolPartCard key={item.key} part={item.part} approve={approve} deny={deny} onPinStock={onPinStock} agentRunRecords={agentRunRecords} onCancelAgentRun={onCancelAgentRun} onInterpretAgentRun={onInterpretAgentRun} />
        return null
      })}
    </>
  )
}

function UserText({ message }: { message: UIMessage }) {
  return message.parts
    .filter((part) => part.type === 'text')
    .map((part) => String((part as MessagePart).text || ''))
    .join('\n')
}

function ToolPartCard({
  part,
  approve,
  deny,
  onPinStock,
  agentRunRecords,
  onCancelAgentRun,
  onInterpretAgentRun,
}: {
  part: ToolPart
  approve: (id: string) => void
  deny: (id: string) => void
  onPinStock: (item: PinStockInput) => void
  agentRunRecords: Record<string, AgentRunRecord>
  onCancelAgentRun: (runId: string) => Promise<void>
  onInterpretAgentRun: (runId: string) => void
}) {
  const { t } = usePreferences()
  const toolName = getToolName(part)
  const stateLabel = toolStateLabel(part, t)
  const initialRun = toolName === 'run_python_research' ? parseAgentRunRecord(part.output) : null
  const sandboxRun = initialRun ? agentRunRecords[initialRun.id] || initialRun : null
  return (
    <div className={`my-2 rounded-md border px-3 py-2 ${toolToneClass(toolName)}`}>
      <div className="flex items-center justify-between gap-3">
        <span className="flex min-w-0 items-center gap-1.5">
          <Wrench size={12} className="shrink-0" />
          <span className="truncate text-[12px] font-medium">{formatToolName(toolName, t)}</span>
        </span>
        <span className="shrink-0 text-[10px] opacity-75">{sandboxRun ? sandboxRunStatusLabel(sandboxRun, t) : stateLabel}</span>
      </div>
      {sandboxRun ? (
        <SandboxRunDetails run={sandboxRun} onCancel={onCancelAgentRun} onInterpret={onInterpretAgentRun} />
      ) : (
        <ToolStructuredOutput toolName={toolName} input={part.input} output={part.output} onPinStock={onPinStock} />
      )}
      {part.errorText && <p className="mt-2 text-xs text-red-700 dark:text-red-200">{part.errorText}</p>}
      {part.state === 'approval-requested' && part.approval?.id && (
        <div className="mt-3 flex items-center gap-2">
          <button type="button" onClick={() => approve(part.approval!.id)} className="inline-flex items-center gap-1 rounded-md bg-red-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-red-700">
            <Check size={12} />
            {t('chat.approve')}
          </button>
          <button type="button" onClick={() => deny(part.approval!.id)} className="inline-flex items-center gap-1 rounded-md border border-border bg-background px-2.5 py-1 text-xs font-medium text-foreground hover:bg-muted/60">
            <X size={12} />
            {t('chat.deny')}
          </button>
        </div>
      )}
    </div>
  )
}

function SandboxRunDetails({
  run,
  onCancel,
  onInterpret,
}: {
  run: AgentRunRecord
  onCancel: (runId: string) => Promise<void>
  onInterpret: (runId: string) => void
}) {
  const { t } = usePreferences()
  const output = run.stdout?.trim()
  const error = run.error || run.stderr?.trim()
  return (
    <div className="mt-2 space-y-2 text-xs leading-5">
      <p>{sandboxRunHint(run, t)}</p>
      {typeof run.exitCode === 'number' && <p className="text-[10px] opacity-75">{t('chat.sandboxExitCode').replace('{code}', String(run.exitCode))}</p>}
      {output && <pre className="max-h-48 overflow-auto rounded bg-background/70 px-2 py-1.5 font-mono text-[11px] leading-4 text-foreground whitespace-pre-wrap">{output}</pre>}
      {error && <pre className="max-h-32 overflow-auto rounded bg-red-950/5 px-2 py-1.5 font-mono text-[11px] leading-4 text-red-800 whitespace-pre-wrap dark:bg-red-500/10 dark:text-red-100">{error}</pre>}
      {run.usage && <p className="text-[10px] opacity-75">{t('chat.sandboxUsage').replace('{cpu}', String(run.usage.activeCpuUsageMs)).replace('{network}', String(run.usage.networkIngressBytes + run.usage.networkEgressBytes))}</p>}
      {run.status === 'queued' && (
        <button type="button" onClick={() => void onCancel(run.id)} className="rounded-md border border-current/30 bg-background/60 px-2 py-1 text-[11px] font-medium hover:bg-background">
          {t('chat.sandboxCancel')}
        </button>
      )}
      {run.status === 'completed' && (
        <button type="button" onClick={() => onInterpret(run.id)} className="rounded-md bg-primary px-2.5 py-1 text-[11px] font-medium text-primary-foreground hover:bg-primary/90">
          {t('chat.sandboxInterpret')}
        </button>
      )}
    </div>
  )
}

function sandboxRunStatusLabel(run: AgentRunRecord, t: ReturnType<typeof usePreferences>['t']): string {
  if (run.status === 'queued') return t('chat.sandboxQueued')
  if (run.status === 'running') return t('chat.sandboxRunning')
  if (run.status === 'completed') return t('chat.sandboxCompleted')
  if (run.status === 'cancelled') return t('chat.sandboxCancelled')
  return t('chat.sandboxFailed')
}

function sandboxRunHint(run: AgentRunRecord, t: ReturnType<typeof usePreferences>['t']): string {
  if (run.status === 'queued') return t('chat.sandboxQueuedHint')
  if (run.status === 'running') return t('chat.sandboxRunningHint')
  if (run.status === 'completed') return t('chat.sandboxCompletedHint')
  if (run.status === 'cancelled') return t('chat.sandboxCancelledHint')
  return t('chat.sandboxFailedHint')
}

function ToolRunSummary({ parts, isActive }: { parts: ToolPart[]; isActive: boolean }) {
  const { t } = usePreferences()
  const hasFailure = parts.some((part) => part.state === 'output-error')
  const wasInterrupted = !isActive && parts.some(isRunningTool)
  return (
    <div className="my-3 overflow-hidden rounded-lg border border-border/70 bg-background text-[12px] text-muted-foreground shadow-sm">
      <div className="flex items-center justify-between gap-3">
        <span className="flex min-w-0 items-center gap-2 px-3 py-2 font-medium text-foreground">
          <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary">
            <Wrench size={13} />
          </span>
          <span className="min-w-0">
            <span className="block truncate">{toolGroupTitle(parts, t)}</span>
            <span className="mt-0.5 block text-[10px] font-normal text-muted-foreground">{parts.length} 个执行步骤</span>
          </span>
        </span>
        <span className={`mr-3 shrink-0 rounded-full px-2 py-0.5 text-[10px] ${toolGroupStateTone(parts, isActive)}`}>{toolGroupState(parts, t, isActive)}</span>
      </div>
      <div className="border-t border-border/60 px-3 py-2">
        <div className="space-y-2">
          {parts.map((part, index) => (
            <ToolProgressStep key={`${part.toolCallId}-${index}`} part={part} isLast={index === parts.length - 1} />
          ))}
        </div>
      </div>
      {(hasFailure || wasInterrupted) && (
        <div className="border-t border-border/60 px-3 py-2">
          {hasFailure && <p className="text-[11px] text-amber-700 dark:text-amber-200">{t('chat.toolPartialFailure')}</p>}
          {wasInterrupted && <p className="text-[11px] text-amber-700 dark:text-amber-200">{t('chat.toolInterruptedHint')}</p>}
        </div>
      )}
    </div>
  )
}

function ToolProgressStep({ part, isLast }: { part: ToolPart; isLast: boolean }) {
  const { t } = usePreferences()
  const toolName = getToolName(part)
  const running = isRunningTool(part)
  const failed = part.state === 'output-error'
  const done = part.state === 'output-available'
  return (
    <div className="grid grid-cols-[24px_minmax(0,1fr)] gap-2">
      <div className="relative flex justify-center">
        <span className={`z-10 flex h-5 w-5 items-center justify-center rounded-full border bg-background ${toolStepMarkerClass(part)}`}>
          {running ? <LoaderCircle size={12} className="animate-spin" /> : failed ? <X size={11} /> : done ? <Check size={11} /> : <Wrench size={11} />}
        </span>
        {!isLast && <span className="absolute top-5 h-[calc(100%+0.5rem)] w-px bg-border" />}
      </div>
      <div className="min-w-0 pb-1.5">
        <div className="flex min-w-0 items-center justify-between gap-2">
          <div className="min-w-0">
            <div className="truncate text-xs font-medium text-foreground">{toolChipLabel(part, t)}</div>
            <div className="mt-0.5 line-clamp-2 text-[11px] leading-4">{toolProgressDescription(toolName, part.input)}</div>
          </div>
          <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] ${toolStepStateTone(part)}`}>{toolStateLabel(part, t)}</span>
        </div>
        {part.errorText && <p className="mt-1 text-[11px] text-red-700 dark:text-red-200">{part.errorText}</p>}
        {part.state === 'output-available' && part.output != null && !STRUCTURED_TOOL_NAMES.has(toolName) && (
          <div className="mt-1 rounded-md bg-muted/50 px-2 py-1 text-[11px] leading-4 text-muted-foreground">
            {toolResultDigest(toolName, part.output)}
          </div>
        )}
      </div>
    </div>
  )
}
