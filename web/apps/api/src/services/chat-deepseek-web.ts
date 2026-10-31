import type { ToolSet } from 'ai'
import {
  buildDeepSeekWebTools,
  DEEPSEEK_STANDALONE_WEB_GUIDANCE,
  officialDeepSeekWebCredential,
  standaloneDeepSeekWebEnabled,
  type DeepSeekWebCredentialConfig,
} from '@wyckoff/shared'

export function readingRoomDeepSeekWeb(input: {
  configs: DeepSeekWebCredentialConfig[]
  memberEnabled: boolean
  providerToolNames: readonly string[]
  fetchImpl: typeof fetch
}): { tools: ToolSet; guidance: string } {
  const credential = officialDeepSeekWebCredential(input.configs)
  const enabled = standaloneDeepSeekWebEnabled(input.providerToolNames.includes('web_search'), credential)
  if (!input.memberEnabled || !enabled || !credential) return { tools: {}, guidance: '' }
  return {
    tools: buildDeepSeekWebTools(credential, input.fetchImpl),
    guidance: DEEPSEEK_STANDALONE_WEB_GUIDANCE,
  }
}
