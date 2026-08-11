import { describe, expect, it } from 'vitest'
import {
  buildSettingsCapabilityRows,
  summarizeSettingsCapabilities,
  type SettingsCapabilityId,
  type SettingsCapabilityRow,
} from '../settings-capabilities'

function findRow(rows: SettingsCapabilityRow[], id: SettingsCapabilityId): SettingsCapabilityRow {
  const row = rows.find((item) => item.id === id)
  expect(row).toBeDefined()
  return row!
}

describe('settings capabilities', () => {
  it('marks key settings missing when credentials are empty', () => {
    const rows = buildSettingsCapabilityRows({
      tickflow: '',
      savedTickflow: '',
      modelProviderLabel: 'DeepSeek',
      modelConfig: { api_key: '', model: '' },
      savedModelConfig: { api_key: '', model: '' },
    })
    const summary = summarizeSettingsCapabilities(rows)

    expect(findRow(rows, 'market-data').status).toBe('missing_config')
    expect(findRow(rows, 'reading-model').status).toBe('missing_config')
    expect(summary).toMatchObject({
      readyCount: 0,
      unsavedCount: 0,
      missingCount: 2,
      totalCount: 2,
      hasCrossMarketKline: false,
      hasReadingModel: false,
      isFullyConfigured: false,
      hasUnsavedChanges: false,
    })
  })

  it('marks filled-but-unsaved credentials as amber unsaved, not ready', () => {
    const rows = buildSettingsCapabilityRows({
      tickflow: ' tf-key ',
      savedTickflow: '',
      modelProviderLabel: 'DeepSeek',
      modelConfig: { api_key: 'deepseek-key', model: 'deepseek-chat' },
      savedModelConfig: { api_key: '', model: '' },
    })
    const tickflow = findRow(rows, 'market-data')
    const model = findRow(rows, 'reading-model')
    const summary = summarizeSettingsCapabilities(rows)

    expect(tickflow.status).toBe('unsaved')
    expect(tickflow.isReady).toBe(false)
    expect(tickflow.isUnsaved).toBe(true)
    expect(model.status).toBe('unsaved')
    expect(summary).toMatchObject({
      readyCount: 0,
      unsavedCount: 2,
      missingCount: 0,
      hasCrossMarketKline: false,
      hasReadingModel: false,
      isFullyConfigured: false,
      hasUnsavedChanges: true,
    })
  })

  it('treats TickFlow as ready only after saved credentials match the draft', () => {
    const rows = buildSettingsCapabilityRows({
      tickflow: ' tf-key ',
      savedTickflow: 'tf-key',
      modelProviderLabel: 'DeepSeek',
      modelConfig: { api_key: '', model: '' },
      savedModelConfig: { api_key: '', model: '' },
    })
    const tickflow = findRow(rows, 'market-data')
    const summary = summarizeSettingsCapabilities(rows)

    expect(tickflow.status).toBe('ready')
    expect(tickflow.isReady).toBe(true)
    expect(tickflow.priority).toBe('primary')
    expect(summary.hasCrossMarketKline).toBe(true)
    expect(summary.readyCount).toBe(1)
  })

  it('requires both an API key and model name for the reading model', () => {
    const missingModelRows = buildSettingsCapabilityRows({
      tickflow: '',
      savedTickflow: '',
      modelProviderLabel: 'DeepSeek',
      modelConfig: { api_key: 'deepseek-key', model: '' },
      savedModelConfig: { api_key: '', model: '' },
    })
    const readyRows = buildSettingsCapabilityRows({
      tickflow: '',
      savedTickflow: '',
      modelProviderLabel: 'DeepSeek',
      modelConfig: { api_key: 'deepseek-key', model: 'deepseek-chat' },
      savedModelConfig: { api_key: 'deepseek-key', model: 'deepseek-chat' },
    })

    expect(findRow(missingModelRows, 'reading-model').status).toBe('missing_config')
    expect(findRow(readyRows, 'reading-model').status).toBe('ready')
    expect(findRow(readyRows, 'reading-model').badgeLabels).toEqual(['deepseek-chat'])
  })

  it('marks model unsaved when provider switched before save', () => {
    const rows = buildSettingsCapabilityRows({
      tickflow: 'tf-key',
      savedTickflow: 'tf-key',
      modelProviderLabel: 'DeepSeek',
      modelConfig: { api_key: 'deepseek-key', model: 'deepseek-chat' },
      savedModelConfig: { api_key: 'old-key', model: 'old-model' },
      providerMatchesSaved: false,
    })

    expect(findRow(rows, 'reading-model').status).toBe('unsaved')
    expect(summarizeSettingsCapabilities(rows).hasUnsavedChanges).toBe(true)
  })

  it('summarizes fully configured key settings', () => {
    const rows = buildSettingsCapabilityRows({
      tickflow: 'tf-key',
      savedTickflow: 'tf-key',
      modelProviderLabel: 'DeepSeek',
      modelConfig: { api_key: 'deepseek-key', model: 'deepseek-chat' },
      savedModelConfig: { api_key: 'deepseek-key', model: 'deepseek-chat' },
    })

    expect(summarizeSettingsCapabilities(rows)).toMatchObject({
      readyCount: 2,
      unsavedCount: 0,
      missingCount: 0,
      totalCount: 2,
      hasCrossMarketKline: true,
      hasReadingModel: true,
      isFullyConfigured: true,
      hasUnsavedChanges: false,
    })
  })
})
