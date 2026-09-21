import { createApiApp } from './app'
import { chatRoutes } from './routes/chat'
import { portfolioRoutes } from './routes/portfolio'
import { settingsRoutes } from './routes/settings'
import { shadowLedgerRoutes } from './routes/shadow-ledger'

// Sandbox-less compatibility app for tests. Production Pages Functions reverse-proxy
// to the full Worker instead of mounting this app.
const app = createApiApp()

app.route('/api/chat', chatRoutes)
app.route('/api/portfolio', portfolioRoutes)
app.route('/api/settings', settingsRoutes)
app.route('/api/shadow-ledger', shadowLedgerRoutes)

export default app
