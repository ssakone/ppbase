import { apiClient } from '../client'
import type { AdminAuth } from '../types'

export interface InitStatus {
  needsSetup: boolean
}

export async function getInitStatus(): Promise<InitStatus> {
  return apiClient.request<InitStatus>('GET', '/api/init')
}

export async function createFirstAdmin(
  email: string,
  password: string,
  passwordConfirm: string,
): Promise<AdminAuth> {
  return apiClient.request<AdminAuth>('POST', '/api/admins/init', {
    email,
    password,
    passwordConfirm,
  })
}
