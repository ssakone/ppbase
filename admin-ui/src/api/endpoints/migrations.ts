import { apiClient } from '../client'
import type { Migration, PaginatedResult, MigrationStatus } from '../types'

export interface MigrationsListParams {
  page?: number
  perPage?: number
}

type LegacyMigrationsResponse = {
  items: Migration[]
  totalItems: number
}

type PaginatedMigrationsResponse = PaginatedResult<Migration>

function isPaginatedMigrationsResponse(
  data: LegacyMigrationsResponse | PaginatedMigrationsResponse,
): data is PaginatedMigrationsResponse {
  return (
    typeof (data as PaginatedMigrationsResponse).page === 'number'
    && typeof (data as PaginatedMigrationsResponse).perPage === 'number'
    && typeof (data as PaginatedMigrationsResponse).totalPages === 'number'
  )
}

export async function listMigrations(params: MigrationsListParams = {}): Promise<PaginatedResult<Migration>> {
  const { page = 1, perPage = 30 } = params
  const qs = new URLSearchParams()
  qs.set('page', String(page))
  qs.set('perPage', String(perPage))

  const data = await apiClient.request<LegacyMigrationsResponse | PaginatedMigrationsResponse>(
    'GET',
    `/api/migrations?${qs}`,
  )

  if (isPaginatedMigrationsResponse(data)) {
    return data
  }

  const totalItems = data.totalItems ?? data.items.length
  const totalPages = Math.max(1, Math.ceil(totalItems / perPage))
  const start = (page - 1) * perPage
  const end = start + perPage

  return {
    page,
    perPage,
    totalItems,
    totalPages,
    items: data.items.slice(start, end),
  }
}

export async function getMigrationStatus(): Promise<MigrationStatus> {
  return apiClient.request<MigrationStatus>('GET', '/api/migrations/status')
}

export async function applyMigrations(): Promise<{ count: number; applied?: string[] }> {
  return apiClient.request<{ count: number; applied?: string[] }>('POST', '/api/migrations/apply')
}

export async function revertMigration(count = 1): Promise<{ count: number; reverted?: string[] }> {
  return apiClient.request<{ count: number; reverted?: string[] }>('POST', '/api/migrations/revert', { count })
}

export async function generateSnapshot(): Promise<{ count: number; generated?: string[] }> {
  return apiClient.request<{ count: number; generated?: string[] }>('POST', '/api/migrations/snapshot')
}
