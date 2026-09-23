import { useEffect } from 'react'
import { supabase } from '@/lib/supabase'
import { useAuthStore } from '@/stores/auth'

export function useSupabaseAuth() {
  const setAuth = useAuthStore((s) => s.setAuth)

  useEffect(() => {
    let active = true
    supabase.auth
      .getSession()
      .then(({ data: { session } }) => {
        if (active) setAuth(session?.user ?? null, session)
      })
      .catch(() => {
        if (active) setAuth(null, null)
      })

    const { data: { subscription } } = supabase.auth.onAuthStateChange((_event, session) => {
      if (active) setAuth(session?.user ?? null, session)
    })

    return () => {
      active = false
      subscription.unsubscribe()
    }
  }, [setAuth])
}
