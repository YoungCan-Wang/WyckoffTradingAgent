'use strict'

const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const ts = require('typescript')

const source = readFileSync(join(__dirname, '..', 'src', 'renderer', 'lib', 'ipc.ts'), 'utf8')
const js = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 }
}).outputText

function loadIpc (events, response = { ok: true, id: 7 }) {
  let listener = null
  global.window = {
    wyckoff: {
      onEvent (handler) {
        listener = handler
        return () => { listener = null }
      },
      call () {
        for (const event of events) listener(event)
        return Promise.resolve(response)
      }
    }
  }
  const mod = { exports: {} }
  new Function('module', 'exports', 'require', js)(mod, mod.exports, require)
  return mod.exports
}

test('result 和 end 早于 invoke 回包时仍能收尾', async () => {
  const { collect } = loadIpc([
    { id: 7, type: 'result', value: 42 },
    { id: 7, type: 'end' }
  ])
  const result = await collect('fast_method')
  assert.equal(result.value, 42)
})

test('早到的后端错误仍由严格调用抛出', async () => {
  const { callWithError } = loadIpc([
    { id: 7, type: 'error', message: '拒绝写入' },
    { id: 7, type: 'end' }
  ])
  await assert.rejects(callWithError('write_method'), /拒绝写入/)
})

test('早到的其他请求事件不会串流', async () => {
  const { collect } = loadIpc([
    { id: 99, type: 'result', value: 'other' },
    { id: 7, type: 'result', value: 'mine' },
    { id: 99, type: 'end' },
    { id: 7, type: 'end' }
  ])
  const result = await collect('fast_method')
  assert.equal(result.value, 'mine')
})
