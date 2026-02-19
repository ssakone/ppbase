import { useQuery } from '@tanstack/react-query'
import { getLogs, getLogStats, getLog, type LogsListParams } from '@/api/endpoints/logs'

export function useLogs(params: LogsListParams = {}) {
  return useQuery({
    queryKey: ['logs', params],
    queryFn: () => getLogs(params),
    retry: false,
  })
}

export function useLogStats() {
  return useQuery({
    queryKey: ['logs', 'stats'],
    queryFn: getLogStats,
    retry: false,
  })
}

export function useLog(id?: string) {
  return useQuery({
    queryKey: ['logs', id],
    queryFn: () => getLog(id!),
    enabled: !!id,
    retry: false,
  })
}
