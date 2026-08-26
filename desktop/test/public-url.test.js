'use strict'

// 内置浏览器的地址闸门。
//
// 这里最重要的一条不是「私网地址被拒」，而是**校验和实际连接必须看同一份
// DNS 结果**。原来用 Node 的 dns.lookup 校验、把 URL 字符串交给 Chromium 去连，
// Chromium 有自己的 host resolver 会独立再解析一次 —— 短 TTL 域名可以让校验
// 拿到公网 IP、连接拿到 169.254.169.254（云厂商元数据），整道防线绕过。
// 现在强制用发起请求的那个 session 解析，共用 Chromium 的 DNS 缓存。
//
// 这些用例全部不碰网络：主机名解析走假 session，字面量地址根本不解析。
const assert = require('node:assert/strict')
const test = require('node:test')
const { isPublicAddress, validatePublicHttpUrl } = require('../src/public-url')

/** 假 session：按域名返回预设地址，并记录解析次数。 */
const fakeSession = (map) => ({
  calls: [],
  async resolveHost (host) {
    this.calls.push(host)
    const addresses = map[host]
    if (!addresses) throw new Error('NXDOMAIN')
    return { endpoints: addresses.map((address) => ({ address })) }
  }
})

test('拒绝内网与保留地址字面量', async () => {
  for (const url of [
    'http://127.0.0.1/',
    'http://169.254.169.254/latest/meta-data/',
    'http://10.0.0.1/',
    'http://172.16.0.1/',
    'http://192.168.1.1/',
    'http://[::1]/',
    'http://[::ffff:127.0.0.1]/',
    'http://[fe80::1]/',
    'http://[fc00::1]/'
  ]) {
    await assert.rejects(validatePublicHttpUrl(url, fakeSession({})), undefined, `应拒绝 ${url}`)
  }
})

test('拒绝带口令与非标准端口', async () => {
  const s = fakeSession({ 'example.com': ['93.184.216.34'] })
  await assert.rejects(validatePublicHttpUrl('https://user:pw@example.com/', s))
  await assert.rejects(validatePublicHttpUrl('https://example.com:8443/', s))
})

test('放行公网地址', () => {
  assert.equal(isPublicAddress('8.8.8.8'), true)
  assert.equal(isPublicAddress('2606:4700:4700::1111'), true)
})

test('域名解析到公网地址时放行，且用传入的 session 解析', async () => {
  const s = fakeSession({ 'example.com': ['93.184.216.34'] })
  assert.equal(
    await validatePublicHttpUrl('https://example.com/path', s),
    'https://example.com/path'
  )
  assert.deepEqual(s.calls, ['example.com'], '必须走 session.resolveHost，不能自己 dns.lookup')
})

test('域名解析到内网地址时拒绝（DNS rebinding 的落点）', async () => {
  const s = fakeSession({ 'evil.example': ['169.254.169.254'] })
  await assert.rejects(
    validatePublicHttpUrl('http://evil.example/latest/meta-data/', s),
    /private or reserved/
  )
})

test('多条 A 记录里只要有一条是内网就整体拒绝', async () => {
  // 一半公网一半内网是 rebinding 的常见形态：赌校验只看第一条
  const s = fakeSession({ 'mixed.example': ['93.184.216.34', '10.0.0.5'] })
  await assert.rejects(validatePublicHttpUrl('https://mixed.example/', s), /private or reserved/)
})

test('没有 session 时拒绝解析域名，而不是退回 dns.lookup', async () => {
  // 少传 session 必须响亮地失败 —— 静默退回 Node 解析就等于这个修复没了
  await assert.rejects(validatePublicHttpUrl('https://example.com/'), /session with resolveHost/)
  await assert.rejects(validatePublicHttpUrl('https://example.com/', {}), /session with resolveHost/)
})

test('字面量地址不需要 session', async () => {
  // 已经是 IP 就没有 rebinding 的余地，不该硬要一个 session
  assert.equal(await validatePublicHttpUrl('https://93.184.216.34/'), 'https://93.184.216.34/')
})

// ---- IPv6 分类 ----
// 原来只看首字符是不是 2/3。那会放行 6to4/Teredo 这类内嵌了 IPv4 的过渡地址，
// 还会把 ::ffff:8.8.8.8（首字符是冒号）误判成私网。

test('IPv6：过渡地址按内嵌的 IPv4 判定', () => {
  // 6to4：第 2~3 组就是内嵌的 IPv4
  assert.equal(isPublicAddress('2002:7f00:1::'), false, '内嵌 127.0.0.1，评审点名的绕过')
  assert.equal(isPublicAddress('2002:0a00:0001::'), false, '内嵌 10.0.0.1')
  assert.equal(isPublicAddress('2002:0808:0808::'), true, '内嵌 8.8.8.8，应放行')
  // Teredo：末两组是按位取反的客户端 IPv4
  assert.equal(isPublicAddress('2001:0:0:0:0:0:ffff:ffff'), false, '内嵌 0.0.0.0')
  // NAT64
  assert.equal(isPublicAddress('64:ff9b::7f00:1'), false, 'NAT64 内嵌 127.0.0.1')
  assert.equal(isPublicAddress('64:ff9b::0808:0808'), true, 'NAT64 内嵌 8.8.8.8')
})

test('IPv6：v4-mapped 公网地址不再被误判成私网', () => {
  assert.equal(isPublicAddress('::ffff:8.8.8.8'), true, '以前首字符是冒号，一律判私网')
  assert.equal(isPublicAddress('::ffff:127.0.0.1'), false)
  assert.equal(isPublicAddress('::ffff:169.254.169.254'), false)
  assert.equal(isPublicAddress('::ffff:192.168.1.1'), false)
})

test('IPv6：各保留段', () => {
  for (const addr of ['::', '::1', 'fe80::1', 'fc00::1', 'fd12:3456::1', 'ff02::1', '2001:db8::1']) {
    assert.equal(isPublicAddress(addr), false, `${addr} 应判私网/保留`)
  }
  // 只有全球单播 2000::/3 是公网
  assert.equal(isPublicAddress('3fff::1'), true)
  assert.equal(isPublicAddress('4000::1'), false, '2000::/3 之外')
  assert.equal(isPublicAddress('1000::1'), false, '2000::/3 之外')
})

test('无法解析的输入一律不算公网', () => {
  for (const junk of ['', 'not-an-ip', 'gggg::1', '1.2.3', '999.1.1.1']) {
    assert.equal(isPublicAddress(junk), false, `${JSON.stringify(junk)} 不该被当成公网`)
  }
})
