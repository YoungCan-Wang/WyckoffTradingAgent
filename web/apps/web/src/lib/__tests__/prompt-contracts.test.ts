import { describe, expect, it } from 'vitest'
import { ANALYSIS_SYSTEM_PROMPT } from '../../routes/analysis'
import { BATTLE_SYSTEM_PROMPT } from '../../routes/stock-battle'
import { PORTFOLIO_SYSTEM_PROMPT } from '../../routes/portfolio'

describe('Frontend Prompt Quality Contracts', () => {
  const prompts = [
    { name: 'Analysis Prompt', text: ANALYSIS_SYSTEM_PROMPT },
    { name: 'Battle Prompt', text: BATTLE_SYSTEM_PROMPT },
    { name: 'Portfolio Prompt', text: PORTFOLIO_SYSTEM_PROMPT },
  ]

  const bannedWords = ['必然', '保证', '无风险', '稳赚', '稳赢', '包赚']
  const requiredKeywords = ['数据来源', '置信度', '失效', '风险']

  prompts.forEach(({ name, text }) => {
    it(`should validate banned words in ${name}`, () => {
      bannedWords.forEach((word) => {
        if (text.includes(word)) {
          const negators = ['严禁', '禁止', '不要', '不得', '绝不', '不能', '不']
          const hasNegation = negators.some((neg) => text.includes(neg))
          expect(hasNegation).toBe(true)
        }
      })
    })

    it(`should validate required keywords in ${name}`, () => {
      const missing = requiredKeywords.filter((keyword) => !text.includes(keyword))
      expect(missing).toEqual([])
    })

    it(`should define evidence confidence in ${name}`, () => {
      if (name === 'Analysis Prompt') {
        expect(text).toContain('威科夫分析大师')
        expect(text).toContain('技术面结论')
        return
      }
      expect(text).toContain('证据支持度')
      expect(text).toContain('action_timing')
    })
  })

  it('keeps portfolio account scope explicit', () => {
    expect(PORTFOLIO_SYSTEM_PROMPT).toContain('capital_scope=account_only')
    expect(PORTFOLIO_SYSTEM_PROMPT).toContain('不代表用户全部可投资资产')
  })
})
