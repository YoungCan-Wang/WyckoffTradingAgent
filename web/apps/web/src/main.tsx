import { StrictMode, Suspense, lazy } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import './app.css'
import { AuthGuard } from '@/components/auth-guard'
import { AppLayout } from '@/routes/layout'
import { LoginPage } from '@/routes/login'
const ChatPage = lazy(() => import('@/routes/chat').then(m => ({ default: m.ChatPage })))
import { WyckoffLoading } from '@/components/loading'
import { ErrorBoundary } from '@/components/error-boundary'
import { AppUpdateGate } from '@/components/app-update-gate'
import { PreferencesProvider } from '@/lib/preferences'

// 手机遥控页。独立外壳 —— 不套 AppLayout（那是桌面侧栏布局），也不进 AuthGuard
// 的重定向流程（未登录时它自己显示「需要重新扫码」而不是跳走）。
const RemotePage = lazy(() => import('@/routes/remote').then(m => ({ default: m.RemotePage })))

const PortfolioPage = lazy(() => import('@/routes/portfolio').then(m => ({ default: m.PortfolioPage })))
const TrackingPage = lazy(() => import('@/routes/tracking').then(m => ({ default: m.TrackingPage })))
const AttributionPage = lazy(() => import('@/routes/attribution').then(m => ({ default: m.AttributionPage })))
const SettingsPage = lazy(() => import('@/routes/settings').then(m => ({ default: m.SettingsPage })))
const AnalysisPage = lazy(() => import('@/routes/analysis').then(m => ({ default: m.AnalysisPage })))
const StockBattlePage = lazy(() => import('@/routes/stock-battle').then(m => ({ default: m.StockBattlePage })))
const HistoryPage = lazy(() => import('@/routes/history').then(m => ({ default: m.HistoryPage })))
const ExportPage = lazy(() => import('@/routes/export').then(m => ({ default: m.ExportPage })))
const FeatureGuidePage = lazy(() => import('@/routes/feature-guide').then(m => ({ default: m.FeatureGuidePage })))

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 30_000, retry: 1 },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
    <QueryClientProvider client={queryClient}>
      <PreferencesProvider>
        <AppUpdateGate />
        <BrowserRouter>
          <Suspense fallback={<WyckoffLoading />}>
            <Routes>
              <Route path="/login" element={<LoginPage />} />
              <Route path="/m" element={<RemotePage />} />
              <Route element={<AuthGuard />}>
                <Route element={<AppLayout />}>
                  <Route index element={<Navigate to="/chat" replace />} />
                  <Route path="/chat" element={<ChatPage />} />
                  <Route path="/portfolio" element={<PortfolioPage />} />
                  <Route path="/tracking" element={<TrackingPage />} />
                  <Route path="/attribution" element={<AttributionPage />} />
                  <Route path="/analysis" element={<AnalysisPage />} />
                  <Route path="/battle" element={<StockBattlePage />} />
                  <Route path="/history" element={<HistoryPage />} />
                  <Route path="/export" element={<ExportPage />} />
                  <Route path="/guide" element={<FeatureGuidePage />} />
                  <Route path="/settings" element={<SettingsPage />} />
                </Route>
              </Route>
            </Routes>
          </Suspense>
        </BrowserRouter>
      </PreferencesProvider>
    </QueryClientProvider>
    </ErrorBoundary>
  </StrictMode>,
)
