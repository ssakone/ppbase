import { apiClient } from '../client'
import type { AdminAuth } from '../types'

export interface InitStatus {
  needsSetup: boolean
}

export async function getInitStatus(setupToken?: string | null): Promise<InitStatus> {
  const url = setupToken ? `/api/init?token=${encodeURIComponent(setupToken)}` : '/api/init'
  return apiClient.request<InitStatus>('GET', url)
}

export async function createFirstAdmin(
  email: string,
  password: string,
  passwordConfirm: string,
  setupToken: string,
): Promise<AdminAuth> {
  return apiClient.request<AdminAuth>('POST', '/api/admins/init', {
    email,
    password,
    passwordConfirm,
    setupToken,
  })
}
