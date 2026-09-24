import { Outlet, Navigate } from 'react-router'
import { useAuthStore } from '@/stores/auth'
import { useSupabaseAuth } from '@/lib/auth-session'

export function AuthGuard() {
  const { user, loading } = useAuthStore()
  useSupabaseAuth()

  if (loading) {
    return (
      <div className="flex h-screen items-center justify-center">
        <div className="animate-spin h-8 w-8 border-2 border-primary border-t-transparent rounded-full" />
      </div>
    )
  }

  if (!user) {
    return <Navigate to="/login" replace />
  }

  return <Outlet />
}
