import { agentRunRoutes } from './routes/agent-runs'
import { createApiApp } from './app'
import { portfolioRoutes } from './routes/portfolio'
import { settingsRoutes } from './routes/settings'
import { workerChatRoutes } from './routes/worker-chat'
import { handleAgentRunQueue } from './services/agent-run-queue'
import type { AgentRunMessage } from './services/agent-run'
import type { Env } from './app'

export type { Env } from './app'

export const app = createApiApp()
app.route('/api/chat', workerChatRoutes)
app.route('/api/agent-runs', agentRunRoutes)
app.route('/api/portfolio', portfolioRoutes)
app.route('/api/settings', settingsRoutes)

export default {
  fetch: app.fetch,
  queue: (batch, env) => handleAgentRunQueue(batch, env),
} satisfies ExportedHandler<Env, AgentRunMessage>
