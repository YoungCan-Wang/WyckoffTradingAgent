import { describe, expect, it } from 'vitest'
import { parseAgentRunInput } from './agent-runs'

describe('Agent run input', () => {
  it('accepts the single research task supported by the MVP', () => {
    expect(parseAgentRunInput({ kind: 'python_research', script: 'print(42)' })).toEqual({
      data: { kind: 'python_research', script: 'print(42)' },
    })
  })

  it('rejects arbitrary task kinds and oversized scripts', () => {
    expect(parseAgentRunInput({ kind: 'shell', script: 'ls' })).toHaveProperty('error')
    expect(parseAgentRunInput({ kind: 'python_research', script: 'x'.repeat(12_001) })).toHaveProperty('error')
  })
})
