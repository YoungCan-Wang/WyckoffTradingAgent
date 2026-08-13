'use strict'

const assert = require('node:assert/strict')
const test = require('node:test')
const { isPublicAddress, validatePublicHttpUrl } = require('../src/public-url')

test('rejects local and reserved IP literals', async () => {
  for (const url of [
    'http://127.0.0.1/',
    'http://169.254.169.254/latest/meta-data/',
    'http://10.0.0.1/',
    'http://[::1]/',
    'http://[::ffff:127.0.0.1]/'
  ]) {
    await assert.rejects(validatePublicHttpUrl(url))
  }
})

test('rejects credentials and non-standard ports', async () => {
  await assert.rejects(validatePublicHttpUrl('https://user:pw@example.com/'))
  await assert.rejects(validatePublicHttpUrl('https://example.com:8443/'))
})

test('allows public IP addresses', () => {
  assert.equal(isPublicAddress('8.8.8.8'), true)
  assert.equal(isPublicAddress('2606:4700:4700::1111'), true)
})

test('allows a public HTTPS URL', async () => {
  assert.equal(await validatePublicHttpUrl('https://example.com/path'), 'https://example.com/path')
})
