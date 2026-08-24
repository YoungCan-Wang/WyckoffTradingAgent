/**
 * 持仓 store 的行为约束。
 *
 * 这些是「单一数据源」重构的护栏 —— 之前首页和持仓页各自 fetch,导致首页
 * 持仓数永远是开机那一刻的值(也就是 0),即使持仓页已经把数据拉下来了。
 */
'use strict'
const test = require('node:test')
const assert = require('node:assert')
const fs = require('node:fs')
const path = require('node:path')

const SRC = (rel) => fs.readFileSync(path.join(__dirname, '..', 'src', 'renderer', rel), 'utf8')

/** 去掉注释再匹配 —— 否则「原来这里自己 collect('portfolio')」这种说明文字会误判。 */
const CODE = (rel) => SRC(rel)
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .split('\n').filter((l) => !l.trim().startsWith('//')).join('\n')

test('首页不再自己拉持仓，改为订阅共享 store', () => {
  const w = CODE('components/Welcome.tsx')
  assert.ok(!/collect\('portfolio'\)/.test(w),
    'Welcome 不该自己 collect portfolio —— ChatView 常驻挂载，那次请求开机跑一次就永不再跑')
  assert.match(w, /usePortfolio\(\)/, '应订阅共享 store')
})

test('usePortfolio 是 store 的视图，不自己持有数据', () => {
  const h = SRC('lib/usePortfolio.ts')
  assert.match(h, /useSyncExternalStore/, '用 React 官方的外部 store 原语')
  // 不该再有自己的 useState 数据副本
  assert.ok(!/useState<Portfolio/.test(h), '数据应在 store 里，不在 hook 里')
})

test('store 快照引用稳定，避免无限重渲染', () => {
  const s = SRC('lib/portfolioStore.ts')
  // getSnapshot 每次渲染都被调用并用 Object.is 比对；返回新对象会死循环
  assert.match(s, /export function getSnapshot[\s\S]{0,120}return snapshot/,
    'getSnapshot 必须返回同一个引用，不能每次构造新对象')
})

test('写缓存用后端回传的 user_id，不是自己问来的', () => {
  // account 读磁盘登录态，portfolio 用会话工具上下文，两者可能不是同一账号。
  // 用前者当 key 会把 A 的持仓存成 B 的。
  const s = SRC('lib/portfolioStore.ts')
  assert.match(s, /payload\.user_id/)
  assert.match(s, /writeCache\(owner/)
})

test('并发请求有序号保护', () => {
  // 账号切换时旧请求可能晚一步回来，把上一个账号的持仓渲染到新账号界面上
  const s = SRC('lib/portfolioStore.ts')
  assert.match(s, /requestSeq/)
  assert.match(s, /seq !== requestSeq/)
})

test('单一缓存分区可以跳过 account 往返', () => {
  // 打包后 Python 冷启动十几秒，那期间磁盘上明明有上次的持仓却显示不出来
  const c = SRC('lib/portfolioCache.ts')
  assert.match(c, /export function readSoleCache/)
  assert.match(c, /keys\.length !== 1/, '多个分区就老实去问账号，不能猜')
  const s = SRC('lib/portfolioStore.ts')
  assert.match(s, /readSoleCache\(\)/)
})

test('加载中与真的没持仓要能区分', () => {
  // 都显示 0 的话，冷启动期间看着就像「你没有持仓」
  const w = SRC('components/Welcome.tsx')
  assert.match(w, /pfUnknown/)
  assert.match(w, /value === null \? '–'/, '未知时显示占位符而不是 0')
})

test('useIpc 结果跨挂载缓存', () => {
  // 页面是条件渲染的（view === 'x' ? <Page/> : null），切走就卸载、切回就重挂载。
  // 没有模块级缓存时那就是每次进页面都重拉 —— 跟踪页 ~200ms 察觉不到，
  // 归因页 1.6 秒还在转，于是看起来「只有归因有问题」，其实两个都在重请求。
  const h = CODE('lib/useIpc.ts')
  assert.match(h, /readIpcCache/, '应读模块级缓存')
  assert.match(h, /writeIpcCache/, '应写模块级缓存')
  // 初始 state 就要读缓存：放在 effect 里会先渲染一帧 loading，视觉上仍然「闪一下」
  const init = h.slice(h.indexOf('useState'), h.indexOf('const [nonce'))
  assert.match(init, /readIpcCache/,
    '缓存要在 useState 惰性初始化里读，不能等 effect —— 否则先渲染一帧 loading')
})

test('空结果也算有效缓存', () => {
  // 用「取出来是不是 undefined」判断命中的话，成功但无数据的 null 会被当成未命中，
  // 于是每次进页面都重拉一个注定为空的请求。
  const c = CODE('lib/ipcCache.ts')
  assert.match(c, /store\.has\(key\)/, '用 has() 判断命中，不是看值是否为空')
})

test('归因正文缓存在模块级，不在组件 state', () => {
  const a = CODE('components/AttributionPage.tsx')
  assert.match(a, /^const bodyCache/m, '正文缓存必须活过组件卸载')
  assert.match(a, /new Set\(Object\.keys\(bodyCache\)\)/,
    '去重集合要用已缓存日期预填，否则重挂载后会把已有正文的日期再请求一遍')
})

test('换账号时清掉所有模块级缓存', () => {
  // 模块级缓存不随组件卸载消失 —— 不显式清就会把上一个人的数据给新登录的人
  const app = CODE('components/App.tsx')
  assert.match(app, /clearIpcCache\(\)/)
  const a = CODE('components/AttributionPage.tsx')
  assert.match(a, /wyckoff:account-changed[\s\S]{0,160}delete bodyCache/,
    '归因正文也要跨账号清理')
})
