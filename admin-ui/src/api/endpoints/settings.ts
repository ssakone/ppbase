import { apiClient } from '../client'
import type { Settings } from '../types'

export async function getSettings(): Promise<Settings> {
  return apiClient.request<Settings>('GET', '/api/settings')
}

export async function updateSettings(data: Partial<Settings>): Promise<Settings> {
  return apiClient.request<Settings>('PATCH', '/api/settings', data)
}
