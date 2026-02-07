import { useQuery } from '@tanstack/react-query'
import { getDatabaseTables } from '@/api/endpoints/collections'

export function useDatabaseTables() {
  return useQuery({
    queryKey: ['database-tables'],
    queryFn: getDatabaseTables,
    staleTime: 5 * 60 * 1000, // 5 minutes
  })
}
