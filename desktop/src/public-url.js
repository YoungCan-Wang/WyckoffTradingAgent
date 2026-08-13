'use strict'

const dns = require('node:dns').promises
const net = require('node:net')

const STANDARD_PORTS = new Set(['', '80', '443'])
const CACHE_TTL_MS = 60_000
const hostCache = new Map()

function isPublicAddress (address, allowProxyFakeIp = false) {
  if (net.isIPv4(address)) {
    const [a, b, c] = address.split('.').map(Number)
    return !(
      a === 0 || a === 10 || a === 127 ||
      (a === 100 && b >= 64 && b <= 127) ||
      (a === 169 && b === 254) ||
      (a === 172 && b >= 16 && b <= 31) ||
      (a === 192 && b === 0) ||
      (a === 192 && b === 168) ||
      (!allowProxyFakeIp && a === 198 && (b === 18 || b === 19)) ||
      (a === 198 && b === 51 && c === 100) ||
      (a === 203 && b === 0 && c === 113) ||
      a >= 224 ||
      (a === 255 && b === 255 && c === 255)
    )
  }
  if (net.isIPv6(address)) {
    const value = address.toLowerCase()
    return value.startsWith('2') || value.startsWith('3')
  }
  return false
}

async function validatePublicHttpUrl (raw) {
  let url
  try {
    url = new URL(String(raw || '').trim())
  } catch {
    throw new Error('invalid URL')
  }
  if (!['http:', 'https:'].includes(url.protocol)) throw new Error('only http(s) URLs are allowed')
  if (url.username || url.password) throw new Error('credentials in URLs are not allowed')
  if (!STANDARD_PORTS.has(url.port)) throw new Error('non-standard ports are not allowed')
  const host = url.hostname.toLowerCase().replace(/^\[|\]$/g, '').replace(/\.$/, '')
  if (host === 'localhost' || host.endsWith('.local')) throw new Error('local addresses are not allowed')

  let records = hostCache.get(host)
  if (!records || records.expiresAt < Date.now()) {
    try {
      const addresses = net.isIP(host) ? [{ address: host }] : await dns.lookup(host, { all: true, verbatim: true })
      records = { addresses, expiresAt: Date.now() + CACHE_TTL_MS }
      hostCache.set(host, records)
    } catch {
      throw new Error('URL host could not be resolved')
    }
  }
  const hostIsLiteral = net.isIP(host) !== 0
  if (!records.addresses.length || records.addresses.some((record) => !isPublicAddress(record.address, !hostIsLiteral))) {
    throw new Error('private or reserved addresses are not allowed')
  }
  return url.toString()
}

module.exports = { isPublicAddress, validatePublicHttpUrl }
