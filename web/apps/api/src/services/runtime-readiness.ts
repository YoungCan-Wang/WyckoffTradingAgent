import type { Env } from '../app'

const MIN_APPROVAL_SECRET_LENGTH = 32
const CHAT_CONFIGURATION_ERROR = '读盘室服务配置异常，请稍后重试。'

export function getToolApprovalSecret(env: Env): string {
  const secret = env.CHAT_TOOL_APPROVAL_SECRET
  if (!secret || secret.trim().length < MIN_APPROVAL_SECRET_LENGTH) {
    throw new Error(CHAT_CONFIGURATION_ERROR)
  }
  return secret
}

export function missingWorkerRuntimeSecrets(env: Env): string[] {
  try {
    getToolApprovalSecret(env)
    return []
  } catch {
    return ['CHAT_TOOL_APPROVAL_SECRET']
  }
}
