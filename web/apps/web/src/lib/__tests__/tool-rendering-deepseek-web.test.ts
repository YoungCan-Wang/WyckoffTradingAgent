import { describe, expect, it } from 'vitest'
import { formatToolName, toolProgressDescription, toolResultDigest } from '@/features/reading-room/tool-rendering-model'

const t = (key: string) => ({
  'tool.deepseek_web_search': 'DeepSeek 网页搜索',
  'tool.deepseek_web_fetch': 'DeepSeek 网页抓取',
}[key] ?? key)

describe('deepseek web tool rendering', () => {
  it('labels search and fetch separately from Responses web_search', () => {
    expect(formatToolName('deepseek_web_search', t)).toBe('DeepSeek 网页搜索')
    expect(formatToolName('deepseek_web_fetch', t)).toBe('DeepSeek 网页抓取')
    expect(formatToolName('deepseek_web_search', t)).not.toBe(formatToolName('web_search', (key) => key))
  })

  it('shows the query or url while running and a short result after completion', () => {
    expect(toolProgressDescription('deepseek_web_search', { query: 'DeepSeek V4' })).toBe('DeepSeek 网页搜索：DeepSeek V4')
    expect(toolProgressDescription('deepseek_web_fetch', { url: 'https://example.com/a' })).toContain('https://example.com/a')
    expect(toolResultDigest('deepseek_web_search', {
      query: 'DeepSeek V4',
      results: [{ url: 'https://example.com/a' }],
      empty: false,
    })).toBe('DeepSeek 搜索完成：DeepSeek V4 · 1 条')
    expect(toolResultDigest('deepseek_web_fetch', {
      title: 'Example',
      url: 'https://example.com/a',
      truncated: true,
    })).toBe('DeepSeek 抓取完成：Example · 已截断')
  })
})
