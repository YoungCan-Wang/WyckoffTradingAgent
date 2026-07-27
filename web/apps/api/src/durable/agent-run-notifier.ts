// Per-user push channel for agent run status. The Worker routes an authenticated WebSocket
// upgrade to /connect, and the queue consumer POSTs status records to /notify, which are
// broadcast to every socket the user has open (multiple tabs included).
// Deliberately not extending DurableObject from cloudflare:workers: the base constructor
// requires a real DurableObjectState, which blocks unit-testing with a fake context.
export class AgentRunNotifier {
  constructor(private readonly ctx: DurableObjectState) {}

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url)
    if (url.pathname === '/connect') return this.upgrade(request)
    if (url.pathname === '/notify' && request.method === 'POST') return this.notify(await request.text())
    return new Response('Not Found', { status: 404 })
  }

  private upgrade(request: Request): Response {
    if (request.headers.get('Upgrade')?.toLowerCase() !== 'websocket') {
      return new Response('Expected a WebSocket upgrade', { status: 426 })
    }
    const pair = new WebSocketPair()
    // Hibernation API: idle sockets are evicted from memory without dropping the connection,
    // so an open tab does not consume Durable Object duration on the free plan.
    this.ctx.acceptWebSocket(pair[1])
    return new Response(null, { status: 101, webSocket: pair[0], headers: acceptedProtocol(request) })
  }

  private notify(payload: string): Response {
    let delivered = 0
    for (const socket of this.ctx.getWebSockets()) {
      try {
        socket.send(payload)
        delivered += 1
      } catch {
        // Stale sockets are reclaimed by the runtime; the frontend keeps polling as fallback.
      }
    }
    return Response.json({ delivered })
  }
}

// Browsers abort the handshake unless the server echoes one of the requested subprotocols.
function acceptedProtocol(request: Request): Record<string, string> {
  const requested = request.headers.get('Sec-WebSocket-Protocol')?.split(',')[0]?.trim()
  return requested ? { 'Sec-WebSocket-Protocol': requested } : {}
}
