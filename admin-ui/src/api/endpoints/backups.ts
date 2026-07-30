import { apiClient, type UploadProgress } from '../client'
import type {
  BackupListItem,
  BackupReadiness,
} from '../types'

function objectValue(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' ? value as Record<string, unknown> : null
}

export function getBackupErrorMessage(error: unknown, fallback: string): string {
  const root = objectValue(error)
  const detail = objectValue(root?.detail)
  const response = objectValue(root?.response)
  const detailData = objectValue(detail?.data)
  const code = typeof detailData?.code === 'string' && detailData.code.trim()
    ? detailData.code.trim()
    : null
  for (const candidate of [detail?.message, root?.message, response?.message]) {
    if (typeof candidate === 'string' && candidate.trim()) {
      const message = candidate.trim()
      return code && !message.includes(code) ? `${message} (${code})` : message
    }
  }
  return code ? `${fallback} (${code})` : fallback
}

export function backupKey(backup: Pick<BackupListItem, 'key'>): string {
  return backup.key
}

export async function listBackups(): Promise<BackupListItem[]> {
  return apiClient.request<BackupListItem[]>('GET', '/api/backups')
}

export function createBackup(name?: string): Promise<void> {
  return apiClient.request<void>('POST', '/api/backups', { name: name || null })
}

export function deleteBackup(id: string): Promise<void> {
  return apiClient.request<void>('DELETE', `/api/backups/${encodeURIComponent(id)}`)
}

export function uploadBackup(
  file: File,
  onProgress?: (progress: UploadProgress) => void,
  signal?: AbortSignal,
): Promise<void> {
  const form = new FormData()
  form.set('file', file)
  return apiClient.requestFormDataWithProgress<void>(
    'POST',
    '/api/backups/upload',
    form,
    onProgress,
    signal,
  )
}

export async function downloadBackup(id: string, filename?: string | null): Promise<void> {
  const { token } = await apiClient.request<{ token: string }>('POST', '/api/files/token')
  if (!token) throw new Error('The server did not return a download token.')
  const url = apiClient.buildUrl(
    `/api/backups/${encodeURIComponent(id)}?token=${encodeURIComponent(token)}`,
  )
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename || ''
  anchor.rel = 'noopener'
  anchor.style.display = 'none'
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
}

export function getBackupReadiness(): Promise<BackupReadiness> {
  return apiClient.request<BackupReadiness>('GET', '/api/backups/readiness')
}

/**
 * Destructively restore a backup into the active PostgreSQL database and local
 * file storage, then let the server restart itself. The server validates the
 * archive fully before mutating anything and returns 202 once the restore has
 * committed and a restart has been scheduled.
 */
export function restoreBackupDestructive(id: string): Promise<Record<string, unknown>> {
  return apiClient.request<Record<string, unknown>>(
    'POST',
    `/api/backups/${encodeURIComponent(id)}/restore-destructive`,
  )
}
