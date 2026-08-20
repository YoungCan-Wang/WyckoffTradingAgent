'use strict'

/**
 * 内置浏览器的地址闸门：只放行公网 http(s)。
 *
 * ## 为什么不用 Node 的 dns.lookup
 *
 * 之前这里用 `dns.lookup` 解析、判断 IP 是否内网，然后把 **URL 字符串** 交给
 * Chromium 去连。但 Chromium 有自己的 host resolver，会**独立再解析一次** ——
 * 校验看到的 IP 和实际连接的 IP 是两回事。短 TTL 域名可以让校验拿到公网地址、
 * 连接时拿到 169.254.169.254（云厂商元数据）或内网地址，整道防线形同虚设
 * （DNS rebinding / TOCTOU）。
 *
 * 现在改用 `session.resolveHost`：它走的是**同一个 session 的 host resolver**，
 * 解析结果进 Chromium 自己的 DNS 缓存，随后那次 loadURL 会复用同一条缓存记录。
 * 这不是理论上的完美钉死（缓存到期、或 Chromium 内部决定重解析时仍有窗口），
 * 但校验与连接终于在看同一份数据，而不是各解析一次。
 *
 * 也因此不再自己维护 hostCache —— 那层缓存只影响我们的判定、对 Chromium 的
 * 实际连接毫无约束，唯一的作用是把攻击窗口从一次请求拉长到 60 秒。
 */

const net = require('node:net')

const STANDARD_PORTS = new Set(['', '80', '443'])

/** 把 IPv6 文本展开成 8 组 16 位整数；解析不了返回 null。 */
function parseIPv6 (input) {
  let text = String(input).toLowerCase().replace(/^\[|\]$/g, '')
  // 去掉 scope id（fe80::1%en0）
  const pct = text.indexOf('%')
  if (pct >= 0) text = text.slice(0, pct)

  // 尾部可能是点分十进制（::ffff:192.168.0.1 / 2002:...:1.2.3.4）。
  // 把它摘掉换成两组十六进制，而不是留一个占位组 —— 占位会让后面所有组
  // 整体错位一位（::ffff:8.8.8.8 里的 ffff 会落到第 4 组而不是第 5 组）。
  let tail = []
  const lastColon = text.lastIndexOf(':')
  const maybeV4 = text.slice(lastColon + 1)
  if (maybeV4.includes('.')) {
    if (!net.isIPv4(maybeV4)) return null
    const [a, b, c, d] = maybeV4.split('.').map(Number)
    tail = [(a << 8) | b, (c << 8) | d]
    // 连同那个分隔冒号一起去掉。'::ffff:' -> '::ffff'，'2002:1:' -> '2002:1'
    text = text.slice(0, lastColon)
  }

  const halves = text.split('::')
  if (halves.length > 2) return null
  const toGroups = (part) => (part ? part.split(':').filter((s) => s !== '') : [])
    .map((s) => (/^[0-9a-f]{1,4}$/.test(s) ? parseInt(s, 16) : NaN))

  const head = toGroups(halves[0])
  const rest = halves.length === 2 ? toGroups(halves[1]) : []
  if ([...head, ...rest].some(Number.isNaN)) return null

  // 点分尾部占最后两组；把它接在显式部分之后
  const explicit = halves.length === 2
    ? [...head, ...new Array(Math.max(0, 8 - head.length - rest.length - tail.length)).fill(0), ...rest, ...tail]
    : [...head, ...tail]
  if (explicit.length !== 8) return null
  return explicit
}

/** IPv4 是否公网。allowProxyFakeIp：198.18/19 是基准测试段，代理场景会用。 */
function isPublicIPv4 (address, allowProxyFakeIp) {
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

/**
 * 地址是否公网。
 *
 * IPv6 以前只看首字符是不是 2/3 —— 那会放行 6to4（2002:7f00:1:: 内嵌
 * 127.0.0.1）和 Teredo（2001::/32），还会把 IPv4-mapped 的公网地址
 * （::ffff:8.8.8.8，首字符是冒号）误判成私网。现在按段判，并且把内嵌了
 * IPv4 的过渡地址**取出内嵌地址递归判一次**。
 */
function isPublicAddress (address, allowProxyFakeIp = false) {
  if (net.isIPv4(address)) return isPublicIPv4(address, allowProxyFakeIp)
  if (!net.isIPv6(address)) return false

  const g = parseIPv6(address)
  if (!g) return false
  const v4 = (hi, lo) => `${hi >> 8}.${hi & 0xff}.${lo >> 8}.${lo & 0xff}`

  // 未指定地址 :: 与回环 ::1
  if (g.every((x) => x === 0)) return false
  if (g.slice(0, 7).every((x) => x === 0) && g[7] === 1) return false

  // IPv4-mapped ::ffff:a.b.c.d 与 IPv4-compatible ::a.b.c.d —— 判内嵌地址
  if (g.slice(0, 5).every((x) => x === 0) && g[5] === 0xffff) {
    return isPublicIPv4(v4(g[6], g[7]), allowProxyFakeIp)
  }
  if (g.slice(0, 6).every((x) => x === 0) && (g[6] || g[7])) {
    return isPublicIPv4(v4(g[6], g[7]), allowProxyFakeIp)
  }
  // NAT64 64:ff9b::/96
  if (g[0] === 0x64 && g[1] === 0xff9b && g[2] === 0 && g[3] === 0 && g[4] === 0 && g[5] === 0) {
    return isPublicIPv4(v4(g[6], g[7]), allowProxyFakeIp)
  }
  // 6to4 2002::/16 —— 第 2~3 组就是内嵌的 IPv4
  if (g[0] === 0x2002) return isPublicIPv4(v4(g[1], g[2]), allowProxyFakeIp)
  // Teredo 2001::/32 —— 末两组是按位取反的客户端 IPv4
  if (g[0] === 0x2001 && g[1] === 0x0000) {
    return isPublicIPv4(v4(~g[6] & 0xffff, ~g[7] & 0xffff), allowProxyFakeIp)
  }
  // 文档段 2001:db8::/32
  if (g[0] === 0x2001 && g[1] === 0x0db8) return false
  // 唯一本地 fc00::/7、链路本地 fe80::/10、组播 ff00::/8
  if ((g[0] & 0xfe00) === 0xfc00) return false
  if ((g[0] & 0xffc0) === 0xfe80) return false
  if ((g[0] & 0xff00) === 0xff00) return false
  // 只有全球单播 2000::/3 是公网
  return (g[0] & 0xe000) === 0x2000
}

/** URL 的静态部分（协议、口令、端口、主机名形态）。不做 DNS。 */
function parseHttpUrl (raw) {
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
  return { url, host }
}

/**
 * 校验并返回可以交给 Chromium 的 URL。
 *
 * @param {string} raw
 * @param {{ resolveHost: (host: string) => Promise<{endpoints: Array<{address: string}>}> }} session
 *   必传：**发起这次请求的那个 session**。用它解析才能与实际连接共用一份 DNS
 *   缓存 —— 换成 Node 的 dns.lookup 就退回到「各解析一次」的可绕过状态。
 */
async function validatePublicHttpUrl (raw, session) {
  const { url, host } = parseHttpUrl(raw)
  const hostIsLiteral = net.isIP(host) !== 0

  let addresses
  if (hostIsLiteral) {
    addresses = [host]
  } else {
    if (!session || typeof session.resolveHost !== 'function') {
      throw new Error('a session with resolveHost is required to validate hostnames')
    }
    try {
      const resolved = await session.resolveHost(host)
      addresses = (resolved.endpoints || []).map((e) => e.address)
    } catch {
      throw new Error('URL host could not be resolved')
    }
  }

  if (!addresses.length || addresses.some((address) => !isPublicAddress(address, !hostIsLiteral))) {
    throw new Error('private or reserved addresses are not allowed')
  }
  return url.toString()
}

module.exports = { isPublicAddress, validatePublicHttpUrl, parseHttpUrl, parseIPv6 }
