import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { BookOpen } from 'lucide-react'
import { supabase } from '@/lib/supabase'
import { useAuthStore } from '@/stores/auth'
import { usePreferences } from '@/lib/preferences'
import { WyckoffLoading } from '@/components/loading'
import { requestShadowLedger } from '@/lib/shadow-ledger-api'
import { shadowCopy } from './copy'
import { ShadowLedgerPanel } from './ledger-panel'
import { ShadowShowcasePanel } from './showcase-panel'

export function ShadowLedgerPage() {
  const { locale } = usePreferences()
  const userId = useAuthStore((state) => state.user?.id)
  const copy = shadowCopy(locale)
  const query = useShadowLedger()
  const [selectedAsOf, setSelectedAsOf] = useState<string | null>(null)
  if (query.isLoading) return <WyckoffLoading />
  if (query.isError || !query.data) {
    return <p className="p-6 text-sm text-destructive">{query.error instanceof Error ? query.error.message : copy.empty}</p>
  }
  const payload = query.data
  const selected = selectedAsOf || payload.asOf
  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-6 p-6">
      <header className="border-b border-border pb-5">
        <div className="flex flex-wrap items-center gap-3">
          <span className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10 text-primary">
            <BookOpen size={20} />
          </span>
          <div className="min-w-0">
            <h1 className="text-xl font-semibold">{copy.title}</h1>
            <p className="mt-1 text-sm text-muted-foreground">{copy.subtitle}</p>
          </div>
          <span className="rounded-full bg-muted px-2.5 py-1 text-xs font-medium">
            {payload.tier === 'full' ? copy.fullBadge : copy.showcaseBadge}
          </span>
        </div>
        <p className="mt-4 text-xs leading-5 text-muted-foreground">{payload.disclaimer || copy.disclaimer}</p>
      </header>
      <ShadowShowcasePanel showcase={payload.showcase} copy={copy} showCta={payload.tier !== 'full'} />
      {payload.tier === 'full' && payload.ledger && (
        <ShadowLedgerPanel
          copy={copy}
          navDaily={payload.ledger.navDaily}
          positions={payload.ledger.positions}
          events={payload.ledger.events}
          selectedAsOf={selected}
          onSelectAsOf={setSelectedAsOf}
        />
      )}
      {!userId && <p className="text-xs text-muted-foreground">{copy.login}</p>}
    </div>
  )
}

function useShadowLedger() {
  const userId = useAuthStore((state) => state.user?.id)
  return useQuery({
    queryKey: ['shadow-ledger', userId || 'guest'],
    queryFn: async () => {
      const { data: { session } } = await supabase.auth.getSession()
      return requestShadowLedger(session?.access_token)
    },
  })
}
