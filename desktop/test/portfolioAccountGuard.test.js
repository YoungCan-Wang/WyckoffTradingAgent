'use strict'

/**
 * 换账号时的持仓账号核对。**行为测试**，不是源码文本断言。
 *
 * 复审两次指出这一块：第一次是「读路径丢弃同步失败、直接渲染旧账号持仓」，
 * 我加了核对但只看 knownUser；第二次指出那个核对在**最需要它的时刻**被跳过 ——
 * `invalidate()` 把 knownUser 置 null 来强制重拉，正好关掉了核对。
 *
 * 这些场景只能靠跑真实 store 才测得出来，所以这里用 ts transpile + 假 IPC。
 */
const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const ts = require('typescript')

const R = (...p) => join(__dirname, '..', 'src', 'renderer', ...p)

/** 把 store 连它的两个依赖一起装进沙箱，返回可控的 IPC。 */
function loadStore () {
  const calls = []
  let nextPortfolio = () => ({ portfolio: { positions: [], free_cash: 0 }, user_id: '' })

  const cache = new Map()
  const fakeModules = {
    './ipc': {
      collect: async (method) => {
        calls.push(method)
        if (method === 'portfolio') return nextPortfolio()
        if (method === 'account') return { user_id: '' }
        return {}
      }
    },
    './portfolioCache': {
      readCache: (uid) => cache.get(uid) || null,
      readSoleCache: () => null,
      writeCache: (uid, p) => {
        const entry = { portfolio: p, savedAt: 1 }
        cache.set(uid, entry)
        return entry
      }
    },
    '../types': {}
  }

  const src = readFileSync(R('lib', 'portfolioStore.ts'), 'utf8')
  const js = ts.transpileModule(src, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 }
  }).outputText

  global.window = { WyckoffI18n: { t: (k) => k } }
  const mod = { exports: {} }
  new Function('module', 'exports', 'require', js)(
    mod, mod.exports, (id) => fakeModules[id] ?? {}
  )
  return { store: mod.exports, calls, setPortfolio: (fn) => { nextPortfolio = fn } }
}

const settle = () => new Promise((r) => setTimeout(r, 30))

test('换账号时后端还返回旧账号的持仓 —— 不能渲染', async () => {
  // 这是复审指出的核心场景：alice → bob，而后端那一轮对话还占着锁，
  // portfolio 仍如实返回 alice 的数据。
  const { store, setPortfolio } = loadStore()
  store.__reset()
  setPortfolio(() => ({
    portfolio: { positions: [{ code: '600519', shares: 100 }], free_cash: 1 },
    user_id: 'alice'
  }))

  store.invalidate('bob')          // 事件里的新账号
  await settle()

  const snap = store.getSnapshot()
  assert.equal(snap.portfolio, null, 'alice 的持仓被渲染给了 bob')
  assert.equal(snap.failed, true, '应该落到失败态并提示重试')
  assert.match(snap.error, /accountMismatch/)
})

test('退出登录后后端还返回上一个账号的持仓 —— 也不能渲染', async () => {
  // 预期账号是空串。我上一版的 `knownUser !== ''` 把这条整个漏掉了。
  const { store, setPortfolio } = loadStore()
  store.__reset()
  setPortfolio(() => ({
    portfolio: { positions: [{ code: '600519', shares: 100 }], free_cash: 1 },
    user_id: 'alice'
  }))

  store.invalidate('')             // 退出登录：应该看到「无账号」的持仓
  await settle()

  assert.equal(store.getSnapshot().portfolio, null, '退出后仍显示 alice 的持仓')
  assert.equal(store.getSnapshot().failed, true)
})

test('账号对得上时正常渲染', async () => {
  const { store, setPortfolio } = loadStore()
  store.__reset()
  setPortfolio(() => ({
    portfolio: { positions: [{ code: '000001', shares: 200 }], free_cash: 5 },
    user_id: 'bob'
  }))

  store.invalidate('bob')
  await settle()

  const snap = store.getSnapshot()
  assert.equal(snap.failed, false, '同账号被误判成不匹配 —— 那会让持仓页整片报错')
  assert.equal(snap.portfolio.positions.length, 1)
})

test('未登录状态下后端也返回空账号 —— 正常渲染', async () => {
  const { store, setPortfolio } = loadStore()
  store.__reset()
  setPortfolio(() => ({
    portfolio: { positions: [], free_cash: 0 },
    user_id: ''
  }))

  store.invalidate('')
  await settle()

  assert.equal(store.getSnapshot().failed, false, '空账号 vs 空账号应该算匹配')
})

test('不匹配时只自动重试一次，不无限循环', async () => {
  // 后端如果一直返回旧账号（长对话），递归 refresh 会变成无限请求。
  const { store, setPortfolio, calls } = loadStore()
  store.__reset()
  setPortfolio(() => ({
    portfolio: { positions: [], free_cash: 0 },
    user_id: 'alice'
  }))

  store.invalidate('bob')
  await settle()

  const portfolioCalls = calls.filter((c) => c === 'portfolio').length
  assert.ok(portfolioCalls <= 2, `请求了 ${portfolioCalls} 次，应该最多 2 次（首次 + 一次重试）`)
  assert.equal(store.getSnapshot().failed, true)
})
