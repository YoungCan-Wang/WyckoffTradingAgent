import type { ReactNode } from 'react'
import { useQuery, type UseQueryResult } from '@tanstack/react-query'
import { getPlanetMembership, type PlanetMembership } from '@/lib/planet-membership'

export function usePlanetMembership(userId: string | undefined) {
  return useQuery({
    queryKey: ['planet-membership', userId],
    queryFn: () => getPlanetMembership(userId || ''),
    enabled: !!userId,
  })
}

export function planetMembershipGateView(
  membership: UseQueryResult<PlanetMembership>,
  loadingView: ReactNode,
  lockedView: ReactNode,
): ReactNode | null {
  if (membership.isLoading) return loadingView
  if (membership.data?.isActive !== true) return lockedView
  return null
}
