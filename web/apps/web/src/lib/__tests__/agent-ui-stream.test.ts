import { DefaultChatTransport, readUIMessageStream, type UIMessage, type UIMessageChunk } from 'ai'
import { describe, expect, it } from 'vitest'
// 与 tests/cli/test_service_ui_stream.py 共用同一份文件：Python 服务产出的字节，
// 必须能被前端实际使用的 ai SDK 客户端（DefaultChatTransport）原样解析。
import golden from '../../../../../../tests/golden/agent_ui_stream.sse?raw'

const USER_MESSAGE: UIMessage = { id: 'u1', role: 'user', parts: [{ type: 'text', text: '看看大盘' }] }

type Part = Record<string, unknown>

async function openStream(body: string): Promise<ReadableStream<UIMessageChunk>> {
  const transport = new DefaultChatTransport<UIMessage>({
    api: 'http://agent.test/api/agent/chat',
    fetch: async () =>
      new Response(body, {
        status: 200,
        headers: { 'content-type': 'text/event-stream', 'x-vercel-ai-ui-message-stream': 'v1' },
      }),
  })
  return transport.sendMessages({
    chatId: 'chat-1',
    messageId: undefined,
    trigger: 'submit-message',
    messages: [USER_MESSAGE],
    abortSignal: undefined,
  })
}

async function lastMessage(stream: ReadableStream<UIMessageChunk>): Promise<UIMessage> {
  let last: UIMessage | undefined
  for await (const message of readUIMessageStream<UIMessage>({ stream })) last = message
  if (!last) throw new Error('stream produced no message')
  return last
}

function partAt(message: UIMessage, index: number): Part {
  const part = message.parts[index]
  if (!part) throw new Error(`missing part ${index}`)
  return part as Part
}

describe('agent service UI message stream', () => {
  it('is parsed by the real ai SDK client into text, step and tool parts', async () => {
    const message = await lastMessage(await openStream(golden))

    expect(message.id).toBe('msg-golden')
    expect(message.role).toBe('assistant')
    expect(message.parts.map((part) => part.type)).toEqual([
      'step-start',
      'text',
      'dynamic-tool',
      'dynamic-tool',
      'step-start',
      'text',
    ])
    expect(partAt(message, 1)).toMatchObject({ text: '我先看一下大盘。', state: 'done' })
    expect(partAt(message, 5)).toMatchObject({ text: '结论：震荡偏弱。', state: 'done' })

    const marketTool = partAt(message, 2)
    expect(marketTool).toMatchObject({
      toolName: 'get_market_overview',
      toolCallId: 'call_1',
      state: 'output-available',
      title: '大盘概览',
    })
    // pandas 算出的 NaN / Infinity 必须在服务端变成 null，否则 JSON.parse 直接失败。
    const output = marketTool.output as Record<string, unknown>
    expect(output.turnover_ratio).toBeNull()
    expect(output.series).toEqual([1, null])
    expect(partAt(message, 3)).toMatchObject({ toolName: 'run_backtest', state: 'output-error', errorText: '数据不足' })
  })

  it('silently drops a chunk with a wrong field name, which is why the assertions above matter', async () => {
    // SDK 对不合 schema 的分块不抛错：坏分块被丢弃，之后的流也不再处理，消息停在 streaming。
    // 服务端字段名写错，用户看到的就是一条空回复。
    const chunks = [
      { type: 'start', messageId: 'm' },
      { type: 'text-start', id: 't1' },
      { type: 'text-delta', id: 't1', text: '字段名写错了' },
      { type: 'text-end', id: 't1' },
    ]
    const body = `${chunks.map((chunk) => `data: ${JSON.stringify(chunk)}\n\n`).join('')}data: [DONE]\n\n`

    const message = await lastMessage(await openStream(body))

    expect(message.parts).toMatchObject([{ type: 'text', text: '', state: 'streaming' }])
  })
})
