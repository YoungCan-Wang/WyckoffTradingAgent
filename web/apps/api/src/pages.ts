import { createApiApp } from './app'
import { chatRoutes } from './routes/chat'
import { portfolioRoutes } from './routes/portfolio'
import { settingsRoutes } from './routes/settings'

// Sandbox-less compatibility app for tests. Production Pages Functions reverse-proxy
// to the full Worker instead of mounting this app.
const app = createApiApp()

app.route('/api/chat', chatRoutes)
app.route('/api/portfolio', portfolioRoutes)
app.route('/api/settings', settingsRoutes)

export default app
