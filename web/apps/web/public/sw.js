/**
 * Service Worker —— 只为了让 PWA 可安装（加到主屏幕）。
 *
 * **刻意不缓存任何东西。** 这是个遥控器：持仓、审批、对话都是实时状态，
 * 缓存过的旧数据比没有数据危险得多 —— 用户可能照着一个小时前的持仓做决定。
 *
 * 装到主屏幕需要一个已注册的 SW（iOS 与 Chrome 的可安装条件），所以它存在，
 * 但对 fetch 一律放行。
 */
self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()))
