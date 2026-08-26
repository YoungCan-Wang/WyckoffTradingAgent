interface ReleaseAsset {
  name: string
  browser_download_url: string
}

interface GithubRelease {
  tag_name: string
  html_url: string
  published_at: string
  draft: boolean
  prerelease: boolean
  assets: ReleaseAsset[]
}

const RELEASES_API = 'https://api.github.com/repos/YoungCan-Wang/WyckoffTradingAgent/releases?per_page=30'

export const onRequest: PagesFunction = async (context) => {
  if (context.request.method !== 'GET') {
    return Response.json({ error: 'method_not_allowed' }, { status: 405 })
  }
  const upstream = await fetch(RELEASES_API, {
    headers: {
      Accept: 'application/vnd.github+json',
      'User-Agent': 'Wyckoff-desktop-update-check',
    },
    cf: { cacheTtl: 300, cacheEverything: true },
  })
  if (!upstream.ok) {
    return Response.json({ error: 'release_lookup_failed' }, { status: 502 })
  }
  const releases = await upstream.json() as GithubRelease[]
  const release = releases.find((item) => (
    !item.draft && !item.prerelease && item.tag_name.startsWith('desktop-v')
  ))
  if (!release) return Response.json({ error: 'no_desktop_release' }, { status: 404 })
  const assets = Object.fromEntries(release.assets.map((item) => [item.name, item.browser_download_url]))
  return Response.json({
    version: release.tag_name.slice('desktop-v'.length),
    tag: release.tag_name,
    release_url: release.html_url,
    published_at: release.published_at,
    assets,
  }, {
    headers: { 'Cache-Control': 'public, max-age=300' },
  })
}
