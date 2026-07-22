import { cloudflareTest } from '@cloudflare/vitest-pool-workers'
import { defineConfig } from 'vitest/config'

const runUpstashIntegration = process.env.RUN_UPSTASH_INTEGRATION === '1'
const runSandboxIntegration = process.env.RUN_VERCEL_SANDBOX_INTEGRATION === '1'

export default defineConfig({
  plugins: [cloudflareTest({
    miniflare: {
      compatibilityDate: '2024-12-18',
      bindings: {
        ENVIRONMENT: 'test',
        CHAT_DAILY_LIMIT_PER_USER: '80',
        CHAT_MIN_INTERVAL_MS: '2500',
        ...(runUpstashIntegration ? {
          RUN_UPSTASH_INTEGRATION: '1',
          UPSTASH_REDIS_REST_URL: process.env.UPSTASH_REDIS_REST_URL || '',
          UPSTASH_REDIS_REST_TOKEN: process.env.UPSTASH_REDIS_REST_TOKEN || '',
        } : {}),
        ...(runSandboxIntegration ? {
          RUN_VERCEL_SANDBOX_INTEGRATION: '1',
          VERCEL_TEAM_ID: process.env.VERCEL_TEAM_ID || '',
          VERCEL_PROJECT_ID: process.env.VERCEL_PROJECT_ID || '',
          VERCEL_TOKEN: process.env.VERCEL_TOKEN || '',
          VERCEL_OIDC_TOKEN: process.env.VERCEL_OIDC_TOKEN || '',
          AGENT_SANDBOX_TIMEOUT_MS: '60000',
        } : {}),
      },
    },
  })],
  test: {
    include: ['src/**/*.test.ts'],
    exclude: ['src/services/python-sandbox.integration.test.ts'],
    restoreMocks: true,
  },
})
