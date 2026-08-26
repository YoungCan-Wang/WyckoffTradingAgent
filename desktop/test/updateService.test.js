'use strict'

const test = require('node:test')
const assert = require('node:assert/strict')
const { checkForDesktopUpdate, isAllowedReleaseUrl, isNewerVersion } = require('../src/update-service')

test('semantic desktop versions compare numerically', () => {
  assert.equal(isNewerVersion('0.10.0', '0.9.9'), true)
  assert.equal(isNewerVersion('1.0.0', '1.0.0'), false)
  assert.equal(isNewerVersion('not-a-version', '1.0.0'), false)
})

test('release links are restricted to this repository', () => {
  assert.equal(
    isAllowedReleaseUrl('https://github.com/YoungCan-Wang/WyckoffTradingAgent/releases/tag/desktop-v0.2.0'),
    true
  )
  assert.equal(isAllowedReleaseUrl('https://example.com/releases/desktop-v0.2.0'), false)
})

test('update manifest is validated before reaching the renderer', async () => {
  const fetchImpl = async () => ({
    ok: true,
    json: async () => ({
      version: '0.2.0',
      release_url: 'https://github.com/YoungCan-Wang/WyckoffTradingAgent/releases/tag/desktop-v0.2.0'
    })
  })
  assert.deepEqual(await checkForDesktopUpdate(fetchImpl, '0.1.0'), {
    ok: true,
    currentVersion: '0.1.0',
    latestVersion: '0.2.0',
    updateAvailable: true,
    releaseUrl: 'https://github.com/YoungCan-Wang/WyckoffTradingAgent/releases/tag/desktop-v0.2.0'
  })
})
