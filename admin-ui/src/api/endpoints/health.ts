import { apiClient } from '../client'
import type { HealthStatus } from '../types'

export async function getHealth(): Promise<HealthStatus> {
  return apiClient.request<HealthStatus>('GET', '/api/health')
}
