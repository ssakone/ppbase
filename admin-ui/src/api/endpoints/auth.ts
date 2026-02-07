import { apiClient } from '../client'
import type { AdminAuth } from '../types'

export async function login(email: string, password: string): Promise<AdminAuth> {
  return apiClient.request<AdminAuth>('POST', '/api/admins/auth-with-password', {
    identity: email,
    password,
  })
}
