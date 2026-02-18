import { apiClient } from '../client'
import type { Settings } from '../types'

export async function getSettings(): Promise<Settings> {
  return apiClient.request<Settings>('GET', '/api/settings')
}

export async function updateSettings(data: Partial<Settings>): Promise<Settings> {
  return apiClient.request<Settings>('PATCH', '/api/settings', data)
}

export type TestEmailTemplate = 'verification' | 'password-reset' | 'email-change'

export async function testSettingsEmail(
  email: string,
  template: TestEmailTemplate,
  collection?: string,
): Promise<void> {
  await apiClient.request<void>('POST', '/api/settings/test/email', {
    email,
    template,
    collection,
  })
}
