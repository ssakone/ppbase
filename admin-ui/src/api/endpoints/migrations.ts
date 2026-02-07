import { apiClient } from '../client'
import type { Migration, MigrationStatus } from '../types'

export async function listMigrations(): Promise<{ items: Migration[]; totalItems: number }> {
  return apiClient.request<{ items: Migration[]; totalItems: number }>('GET', '/api/migrations')
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
