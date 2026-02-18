import { apiClient } from '../client'
import type { LogRecord, LogStats, PaginatedResult } from '../types'

export interface LogsListParams {
  page?: number
  perPage?: number
  filter?: string
  sort?: string
}

export async function getLogs(params: LogsListParams = {}): Promise<PaginatedResult<LogRecord>> {
  const { page = 1, perPage = 30, filter, sort = '-created' } = params
  const qs = new URLSearchParams()
  qs.set('page', String(page))
  qs.set('perPage', String(perPage))
  qs.set('sort', sort)
  if (filter) qs.set('filter', filter)
  return apiClient.request<PaginatedResult<LogRecord>>('GET', `/api/logs?${qs}`)
}

export async function getLogStats(): Promise<LogStats> {
  return apiClient.request<LogStats>('GET', '/api/logs/stats')
}

export async function getLog(id: string): Promise<LogRecord> {
  return apiClient.request<LogRecord>('GET', `/api/logs/${id}`)
}
