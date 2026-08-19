'use strict'

// 评审 P1-1：缓存按账号分区。用固定 key 时 A 退出、B 登录，B 会看到 A 的持仓，
// 而且因为命中缓存不请求后端，这个错不会被纠正。
const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const ts = require('typescript')

// 极简 localStorage，够跑分区逻辑
class FakeStorage {
  constructor () { this.map = new Map() }
  getItem (k) { return this.map.has(k) ? this.map.get(k) : null }
  setItem (k, v) { this.map.set(k, String(v)) }
  removeItem (k) { this.map.delete(k) }
  key (i) { return [...this.map.keys()][i] ?? null }
  get length () { return this.map.size }
}

function load () {
  const src = join(__dirname, '..', 'src', 'renderer', 'lib', 'portfolioCache.ts')
  const js = ts.transpileModule(readFileSync(src, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 }
  }).outputText
  const storage = new FakeStorage()
  global.localStorage = storage
  const mod = { exports: {} }
  new Function('module', 'exports', 'require', js)(mod, mod.exports, require)
  return { ...mod.exports, storage }
}

const pf = (code) => ({ positions: [{ code, name: 'x', shares: 1, cost_price: 1, stop_loss: null }], free_cash: 0, total_equity: 0 })

test('不同账号的缓存互不可见', () => {
  const { readCache, writeCache } = load()
  writeCache('user-A', pf('600519'))
  // B 不该读到 A 的持仓 —— 这是原来那个 bug 的核心
  assert.equal(readCache('user-B'), null, 'B 读到了 A 的缓存')
  assert.equal(readCache('user-A').portfolio.positions[0].code, '600519')
})

test('未登录与已登录分开存', () => {
  const { readCache, writeCache } = load()
  writeCache('', pf('000001'))
  assert.equal(readCache('user-A'), null)
  assert.equal(readCache('').portfolio.positions[0].code, '000001')
})

test('userId 与 key 不符时整条作废', () => {
  const { readCache, storage } = load()
  // 伪造一条：key 是 A 的，内容标着 B —— 只可能是被外部改过
  storage.setItem('wyckoff.portfolio.cache.user-A', JSON.stringify({
    savedAt: Date.now(), userId: 'user-B', portfolio: pf('600519')
  }))
  assert.equal(readCache('user-A'), null, '账号对不上仍然渲染了')
})

test('clearCache 只清指定账号', () => {
  const { readCache, writeCache, clearCache } = load()
  writeCache('user-A', pf('600519'))
  writeCache('user-B', pf('000001'))
  clearCache('user-A')
  assert.equal(readCache('user-A'), null)
  assert.ok(readCache('user-B'), 'B 的缓存被误清了')
})

test('clearAllCaches 清掉所有账号', () => {
  const { readCache, writeCache, clearAllCaches } = load()
  writeCache('user-A', pf('600519'))
  writeCache('user-B', pf('000001'))
  writeCache('', pf('300750'))
  clearAllCaches()
  for (const uid of ['user-A', 'user-B', '']) {
    assert.equal(readCache(uid), null, `${uid || '匿名'} 的缓存没清掉`)
  }
})

test('clearAllCaches 不碰其他 key', () => {
  const { writeCache, clearAllCaches, storage } = load()
  storage.setItem('wyckoff.sidebar', '1')
  storage.setItem('wyckoff.lang', 'zh')
  writeCache('user-A', pf('600519'))
  clearAllCaches()
  assert.equal(storage.getItem('wyckoff.sidebar'), '1')
  assert.equal(storage.getItem('wyckoff.lang'), 'zh')
})

test('损坏的缓存当作没有', () => {
  const { readCache, storage } = load()
  storage.setItem('wyckoff.portfolio.cache.user-A', '{ not json')
  assert.equal(readCache('user-A'), null)
  storage.setItem('wyckoff.portfolio.cache.user-A', JSON.stringify({ savedAt: 1, userId: 'user-A' }))
  assert.equal(readCache('user-A'), null, '缺 portfolio 也该丢掉')
})
