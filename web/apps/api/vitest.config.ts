import { cloudflareTest } from '@cloudflare/vitest-pool-workers'
import { defineConfig } from 'vitest/config'

const runUpstashIntegration = process.env.RUN_UPSTASH_INTEGRATION === '1'

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
      },
    },
  })],
  test: {
    include: ['src/**/*.test.ts'],
    restoreMocks: true,
  },
})
