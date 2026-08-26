'use strict'

const { join } = require('node:path')
const { spawnSync } = require('node:child_process')

module.exports = async function afterPack (context) {
  if (process.env.WYCKOFF_ADHOC_SIGN !== '1' || context.electronPlatformName !== 'darwin') return
  const app = join(context.appOutDir, `${context.packager.appInfo.productFilename}.app`)
  const entitlements = join(context.packager.projectDir, 'build', 'entitlements.mac.plist')
  const result = spawnSync(
    'codesign',
    ['--force', '--deep', '--sign', '-', '--options', 'runtime', '--entitlements', entitlements, app],
    { stdio: 'inherit' }
  )
  if (result.error) throw result.error
  if (result.status !== 0) throw new Error(`ad-hoc codesign failed with status ${result.status}`)
}
