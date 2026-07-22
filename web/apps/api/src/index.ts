import { agentRunRoutes } from './routes/agent-runs'
import { createApiApp } from './app'
import { portfolioRoutes } from './routes/portfolio'
import { settingsRoutes } from './routes/settings'
import { workerChatRoutes } from './routes/worker-chat'

export type { Env } from './app'

const app = createApiApp()
app.route('/api/chat', workerChatRoutes)
app.route('/api/agent-runs', agentRunRoutes)
app.route('/api/portfolio', portfolioRoutes)
app.route('/api/settings', settingsRoutes)

export default app
