'use strict'

const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')
const pkg = require('../package.json')

test('desktop packaging uses a production-size PNG icon', () => {
  assert.equal(pkg.build.icon, 'build/icon.png')
  const icon = fs.readFileSync(path.join(__dirname, '..', pkg.build.icon))
  assert.deepEqual([...icon.subarray(0, 8)], [137, 80, 78, 71, 13, 10, 26, 10])
  assert.equal(icon.readUInt32BE(16), 1024)
  assert.equal(icon.readUInt32BE(20), 1024)
  assert.equal(icon[25], 6)
})
