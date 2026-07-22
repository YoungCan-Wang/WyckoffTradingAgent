import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    include: ['src/services/python-sandbox.integration.test.ts'],
    hookTimeout: 120_000,
    testTimeout: 120_000,
  },
})
