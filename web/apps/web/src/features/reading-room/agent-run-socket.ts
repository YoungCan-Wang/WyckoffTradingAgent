import { useEffect, useRef, useState } from 'react'
import { apiUrl } from '@/lib/api-url'
import { parseAgentRunRecord, type AgentRunRecord } from './agent-runs'

export function agentRunSocketUrl(): string {
  return apiUrl('/api/agent-runs/ws').replace(/^http/, 'ws')
}

export function parseAgentRunPush(data: unknown): AgentRunRecord | null {
  if (typeof data !== 'string') return null
  try {
    return parseAgentRunRecord(JSON.parse(data))
  } catch {
    return null
  }
}

// Opens a push channel for agent run status while runs are active. Returns whether the
// socket is currently connected so the caller can relax its polling fallback. A dropped
// socket is not re-dialed: polling covers the gap until the next active period.
export function useAgentRunSocket(
  token: string | undefined,
  active: boolean,
  onRecord: (record: AgentRunRecord) => void,
): boolean {
  const [connected, setConnected] = useState(false)
  const onRecordRef = useRef(onRecord)
  useEffect(() => { onRecordRef.current = onRecord }, [onRecord])

  useEffect(() => {
    if (!token || !active) return
    let socket: WebSocket
    try {
      // The browser WebSocket API cannot send Authorization headers, so the token rides
      // along as a subprotocol; the Worker validates it before upgrading.
      socket = new WebSocket(agentRunSocketUrl(), ['bearer', token])
    } catch {
      return
    }
    socket.onopen = () => setConnected(true)
    socket.onmessage = (event) => {
      const record = parseAgentRunPush(event.data)
      if (record) onRecordRef.current(record)
    }
    socket.onclose = () => setConnected(false)
    socket.onerror = () => setConnected(false)
    return () => {
      setConnected(false)
      socket.onopen = null
      socket.onmessage = null
      socket.onclose = null
      socket.onerror = null
      socket.close()
    }
  }, [active, token])

  return connected
}
