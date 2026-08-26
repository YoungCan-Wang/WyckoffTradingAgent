'use strict'

const LATEST_URL = 'https://wyckoff-analysis.pages.dev/desktop/latest'
const RELEASE_PREFIX = '/YoungCan-Wang/WyckoffTradingAgent/releases/'

function versionParts (value) {
  if (!/^\d+\.\d+\.\d+$/.test(String(value || ''))) return null
  return String(value).split('.').map(Number)
}

function isNewerVersion (latest, current) {
  const left = versionParts(latest)
  const right = versionParts(current)
  if (!left || !right) return false
  for (let index = 0; index < left.length; index += 1) {
    if (left[index] !== right[index]) return left[index] > right[index]
  }
  return false
}

function isAllowedReleaseUrl (value) {
  try {
    const url = new URL(String(value || ''))
    return url.protocol === 'https:' && url.hostname === 'github.com' && url.pathname.startsWith(RELEASE_PREFIX)
  } catch {
    return false
  }
}

async function checkForDesktopUpdate (fetchImpl, currentVersion) {
  try {
    const response = await fetchImpl(LATEST_URL)
    if (!response.ok) return { ok: false, currentVersion }
    const manifest = await response.json()
    const latestVersion = String(manifest.version || '')
    const releaseUrl = String(manifest.release_url || '')
    if (!versionParts(latestVersion) || !isAllowedReleaseUrl(releaseUrl)) {
      return { ok: false, currentVersion }
    }
    return {
      ok: true,
      currentVersion,
      latestVersion,
      updateAvailable: isNewerVersion(latestVersion, currentVersion),
      releaseUrl
    }
  } catch {
    return { ok: false, currentVersion }
  }
}

module.exports = { checkForDesktopUpdate, isAllowedReleaseUrl, isNewerVersion }
