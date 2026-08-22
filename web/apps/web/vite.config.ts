import { normalizeGeminiStream } from '../../packages/shared/src/gemini-sse-normalize'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import fs from 'node:fs/promises'
import { Readable } from 'node:stream'
import path from 'path'
import type { Plugin } from 'vite'

const GEMINI_ORIGIN = 'https://generativelanguage.googleapis.com'
const SSE_CONTENT_RE = /\btext\/event-stream\b/i

const MARKET_DATA_FILES = new Set([
  'stock_list_cache.json',
  'us_meta.json',
  'hk_meta.json',
  'etf_cn_meta.json',
  'aliases.json',
])
const REPO_DATA_DIR = path.resolve(__dirname, '../../..', 'data')
const MARKET_UNIVERSE_DIR = path.join(REPO_DATA_DIR, 'market_universes')
const BUILD_VERSION = process.env.CF_PAGES_COMMIT_SHA || process.env.VITE_APP_VERSION || `local-${new Date().toISOString()}`
const BUILD_TIME = new Date().toISOString()

function sourceForMarketDataFile(file: string): string {
  return file === 'stock_list_cache.json'
    ? path.join(REPO_DATA_DIR, file)
    : path.join(MARKET_UNIVERSE_DIR, file)
}

function llmProxyPlugin(): Plugin {
  return {
    name: 'llm-proxy',
    configureServer(server) {
      server.middlewares.use('/api/llm-proxy', async (req, res) => {
        const targetUrl = req.headers['x-target-url'] as string
        if (!targetUrl) {
          res.statusCode = 400
          res.end(JSON.stringify({ error: 'Missing X-Target-URL header' }))
          return
        }

        const url = targetUrl + (req.url || '')
        const headers: Record<string, string> = {}
        for (const [key, value] of Object.entries(req.headers)) {
          if (key === 'host' || key === 'x-target-url' || key === 'connection') continue
          if (value) headers[key] = Array.isArray(value) ? value[0]! : value
        }

        try {
          const chunks: Buffer[] = []
          await new Promise<void>((resolve) => {
            req.on('data', (chunk) => { chunks.push(Buffer.from(chunk)) })
            req.on('end', () => resolve())
          })
          const body = Buffer.concat(chunks)

          const fetchHeaders: Record<string, string> = {}
          for (const [key, value] of Object.entries(headers)) {
            if (key === 'content-length' || key === 'user-agent') continue
            fetchHeaders[key] = value
          }
          fetchHeaders['content-length'] = String(body.length)
          fetchHeaders['user-agent'] = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'

          const response = await fetch(url, {
            method: req.method || 'POST',
            headers: fetchHeaders,
            body: body.length > 0 ? body : undefined,
          })

          res.statusCode = response.status
          for (const [key, value] of response.headers.entries()) {
            if (key === 'transfer-encoding' || key === 'content-encoding') continue
            res.setHeader(key, value)
          }

          const contentType = response.headers.get('content-type') || ''
          const proxyPath = (req.url || '').split('?')[0] || ''
          const isGeminiChat = targetUrl.startsWith(GEMINI_ORIGIN) && proxyPath.endsWith('/chat/completions')
          if (isGeminiChat && SSE_CONTENT_RE.test(contentType) && response.body) {
            Readable.fromWeb(normalizeGeminiStream(response.body)).pipe(res)
            return
          }

          const responseBody = await response.arrayBuffer()
          res.end(Buffer.from(responseBody))
        } catch (err: unknown) {
          const msg = err instanceof Error ? err.message : String(err)
          console.error(`[llm-proxy] ERROR: ${msg}`)
          res.statusCode = 502
          res.end(JSON.stringify({ error: { message: `Proxy error → ${url}: ${msg}` } }))
        }
      })
    },
  }
}

function marketDataPlugin(): Plugin {
  return {
    name: 'market-data',
    configureServer(server) {
      server.middlewares.use('/market-data', async (req, res, next) => {
        const url = new URL(req.url || '/', 'http://localhost')
        const file = path.basename(url.pathname)
        if (!MARKET_DATA_FILES.has(file)) {
          next()
          return
        }

        try {
          const body = await fs.readFile(sourceForMarketDataFile(file))
          res.statusCode = 200
          res.setHeader('content-type', 'application/json; charset=utf-8')
          res.setHeader('cache-control', 'public, max-age=300')
          res.end(body)
        } catch (err: unknown) {
          res.statusCode = 404
          res.end(JSON.stringify({ error: err instanceof Error ? err.message : String(err) }))
        }
      })
    },
    async writeBundle() {
      const outDir = path.resolve(__dirname, 'dist', 'market-data')
      await fs.mkdir(outDir, { recursive: true })
      await Promise.all(
        Array.from(MARKET_DATA_FILES, (file) => fs.copyFile(sourceForMarketDataFile(file), path.join(outDir, file))),
      )
    },
  }
}

function appVersionPlugin(): Plugin {
  let outDir = path.resolve(__dirname, 'dist')
  return {
    name: 'app-version',
    configResolved(config) {
      outDir = path.resolve(config.root, config.build.outDir)
    },
    async writeBundle() {
      await fs.mkdir(outDir, { recursive: true })
      await fs.writeFile(
        path.join(outDir, 'version.json'),
        JSON.stringify({ version: BUILD_VERSION, buildTime: BUILD_TIME }, null, 2),
      )
    },
  }
}

function newsEventsPlugin(): Plugin {
  return {
    name: 'news-events',
    configureServer(server) {
      server.middlewares.use('/api/news-events', async (req, res) => {
        const { handleNewsEventsRequest } = await import('../../packages/shared/src/news-chart-events')
        const request = new Request(`http://${req.headers.host || 'localhost'}${req.url || '/api/news-events'}`)
        const response = await handleNewsEventsRequest(request)
        res.statusCode = response.status
        res.setHeader('content-type', 'application/json; charset=utf-8')
        res.end(await response.text())
      })
    },
  }
}

export default defineConfig({
  plugins: [react(), tailwindcss(), llmProxyPlugin(), marketDataPlugin(), appVersionPlugin(), newsEventsPlugin()],
  define: {
    __APP_VERSION__: JSON.stringify(BUILD_VERSION),
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5173,
  },
})
