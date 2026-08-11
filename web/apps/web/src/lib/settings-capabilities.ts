import type { TranslationKey } from './preferences'

export type SettingsCapabilityId = 'market-data' | 'reading-model'
export type SettingsCapabilityStatus = 'ready' | 'unsaved' | 'missing_config'
export type SettingsCapabilityPriority = 'primary'

export interface SettingsCapabilityCredentials {
  api_key?: string | null
  model?: string | null
  base_url?: string | null
}

export interface SettingsCapabilityInput {
  tickflow?: string | null
  savedTickflow?: string | null
  modelProviderLabel: string
  modelConfig?: SettingsCapabilityCredentials
  savedModelConfig?: SettingsCapabilityCredentials
  /** False when draft chat provider differs from last saved provider. */
  providerMatchesSaved?: boolean
}

export interface SettingsCapabilityDefinition {
  id: SettingsCapabilityId
  name: string
  priority: SettingsCapabilityPriority
  priorityLabelKey: TranslationKey
  badgeLabelKeys: readonly TranslationKey[]
  badgeLabels?: readonly string[]
  capabilityLabelKeys: readonly TranslationKey[]
  noteKey: TranslationKey
}

export interface SettingsCapabilityRow extends SettingsCapabilityDefinition {
  status: SettingsCapabilityStatus
  statusLabelKey: TranslationKey
  /** True only when credentials are filled AND match the last saved snapshot. */
  isReady: boolean
  isUnsaved: boolean
}

export interface SettingsCapabilitySummary {
  readyCount: number
  unsavedCount: number
  missingCount: number
  totalCount: number
  hasCrossMarketKline: boolean
  hasReadingModel: boolean
  isFullyConfigured: boolean
  hasUnsavedChanges: boolean
}

const MARKET_DATA_CAPABILITY = {
  id: 'market-data',
  name: 'TickFlow',
  priority: 'primary',
  priorityLabelKey: 'settings.capabilityCategoryData',
  badgeLabelKeys: ['settings.marketCn', 'settings.marketUs', 'settings.marketHk'],
  capabilityLabelKeys: [
    'settings.capabilityDailyKline',
    'settings.capabilityFundamentals',
    'settings.capabilityExport',
    'settings.capabilityCrossMarket',
  ],
  noteKey: 'settings.tickflowCapabilityNote',
} satisfies SettingsCapabilityDefinition

export function buildSettingsCapabilityRows(input: SettingsCapabilityInput): SettingsCapabilityRow[] {
  const providerMatchesSaved = input.providerMatchesSaved !== false
  return [
    buildCredentialRow(
      MARKET_DATA_CAPABILITY,
      hasCredential(input.tickflow),
      hasCredential(input.savedTickflow),
      normalize(input.tickflow) === normalize(input.savedTickflow),
    ),
    buildCredentialRow(
      {
        id: 'reading-model',
        name: input.modelProviderLabel,
        priority: 'primary',
        priorityLabelKey: 'settings.capabilityCategoryModel',
        badgeLabelKeys: [],
        badgeLabels: input.modelConfig?.model?.trim() ? [input.modelConfig.model.trim()] : [],
        capabilityLabelKeys: [
          'settings.capabilityReadingRoom',
          'settings.capabilityAiReport',
          'settings.capabilityStrategyReasoning',
        ],
        noteKey: 'settings.modelCapabilityNote',
      },
      isModelConfigured(input.modelConfig),
      providerMatchesSaved && isModelConfigured(input.savedModelConfig),
      providerMatchesSaved && sameModelCredentials(input.modelConfig, input.savedModelConfig),
    ),
  ]
}

export function summarizeSettingsCapabilities(rows: readonly SettingsCapabilityRow[]): SettingsCapabilitySummary {
  const readyCount = rows.filter((row) => row.status === 'ready').length
  const unsavedCount = rows.filter((row) => row.status === 'unsaved').length
  const totalCount = rows.length
  return {
    readyCount,
    unsavedCount,
    missingCount: totalCount - readyCount - unsavedCount,
    totalCount,
    hasCrossMarketKline: rows.some((row) => row.id === 'market-data' && row.status === 'ready'),
    hasReadingModel: rows.some((row) => row.id === 'reading-model' && row.status === 'ready'),
    isFullyConfigured: readyCount === totalCount,
    hasUnsavedChanges: unsavedCount > 0,
  }
}

function buildCredentialRow(
  definition: SettingsCapabilityDefinition,
  draftReady: boolean,
  savedReady: boolean,
  matchesSaved: boolean,
): SettingsCapabilityRow {
  const status = resolveStatus(draftReady, savedReady, matchesSaved)
  return {
    ...definition,
    status,
    statusLabelKey: statusLabel(status),
    isReady: status === 'ready',
    isUnsaved: status === 'unsaved',
  }
}

function resolveStatus(draftReady: boolean, savedReady: boolean, matchesSaved: boolean): SettingsCapabilityStatus {
  if (!draftReady) return 'missing_config'
  if (savedReady && matchesSaved) return 'ready'
  return 'unsaved'
}

function statusLabel(status: SettingsCapabilityStatus): TranslationKey {
  if (status === 'ready') return 'settings.capabilityReady'
  if (status === 'unsaved') return 'settings.capabilityUnsaved'
  return 'settings.capabilityMissingConfig'
}

function isModelConfigured(modelConfig: SettingsCapabilityCredentials | undefined): boolean {
  return hasCredential(modelConfig?.api_key) && hasCredential(modelConfig?.model)
}

function sameModelCredentials(
  draft: SettingsCapabilityCredentials | undefined,
  saved: SettingsCapabilityCredentials | undefined,
): boolean {
  return (
    normalize(draft?.api_key) === normalize(saved?.api_key)
    && normalize(draft?.model) === normalize(saved?.model)
    && normalize(draft?.base_url) === normalize(saved?.base_url)
  )
}

function hasCredential(value: string | null | undefined): boolean {
  return normalize(value).length > 0
}

function normalize(value: string | null | undefined): string {
  return typeof value === 'string' ? value.trim() : ''
}
