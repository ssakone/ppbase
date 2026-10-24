import { apiClient } from '../client'
import { filterRedactedProps } from '../../lib/redacted-settings'
import type { Settings } from '../types'

export async function getSettings(): Promise<Settings> {
  return apiClient.request<Settings>('GET', '/api/settings')
}

// Secrets come back masked: never send the mask, the server keeps the stored
// secret of an omitted prop.
export async function updateSettings(data: Partial<Settings>): Promise<Settings> {
  return apiClient.request<Settings>('PATCH', '/api/settings', filterRedactedProps(data))
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
