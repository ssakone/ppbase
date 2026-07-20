import { apiClient, type UploadProgress } from '../client'
import type {
  BackupActivation,
  BackupActivationStart,
  BackupIdentity,
  BackupInspection,
  BackupListItem,
  BackupReadiness,
  BackupStagingPlan,
  BackupTrustEntry,
  JwtSecretMode,
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

export function backupKey(backup: Pick<BackupListItem, 'id' | 'key'>): string {
  return backup.id || backup.key
}

export async function listBackups(): Promise<BackupListItem[]> {
  const items = await apiClient.request<BackupListItem[]>('GET', '/api/backups')
  return items.map((item) => ({
    ...item,
    id: item.id || item.key,
    key: item.key || item.id,
  }))
}

export function createBackup(name?: string): Promise<BackupInspection> {
  return apiClient.request<BackupInspection>('POST', '/api/backups', { name: name || null })
}

export function inspectBackup(
  id: string,
  resourceOffset = 0,
  resourceLimit = 100,
): Promise<BackupInspection> {
  const query = new URLSearchParams({
    resourceOffset: String(resourceOffset),
    resourceLimit: String(resourceLimit),
  })
  return apiClient.request<BackupInspection>(
    'GET',
    `/api/backups/${encodeURIComponent(id)}?${query.toString()}`,
  )
}

export function deleteBackup(id: string): Promise<void> {
  return apiClient.request<void>('DELETE', `/api/backups/${encodeURIComponent(id)}`)
}

export function uploadBackup(
  file: File,
  onProgress?: (progress: UploadProgress) => void,
  signal?: AbortSignal,
): Promise<BackupInspection> {
  const form = new FormData()
  form.set('file', file)
  return apiClient.requestFormDataWithProgress<BackupInspection>(
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

export function getBackupIdentity(): Promise<BackupIdentity> {
  return apiClient.request<BackupIdentity>('GET', '/api/backups/identity')
}

export function getBackupReadiness(): Promise<BackupReadiness> {
  return apiClient.request<BackupReadiness>('GET', '/api/backups/readiness')
}

export function listBackupTrust(): Promise<BackupTrustEntry[]> {
  return apiClient.request<BackupTrustEntry[]>('GET', '/api/backups/trusted-signers')
}

export function approveBackupSigner(
  id: string,
  fingerprintSha256: string,
): Promise<BackupTrustEntry | BackupInspection> {
  return apiClient.request<BackupTrustEntry | BackupInspection>(
    'POST',
    `/api/backups/${encodeURIComponent(id)}/trust`,
    { fingerprintSha256 },
  )
}

export function revokeBackupSigner(fingerprint: string): Promise<void> {
  return apiClient.request<void>(
    'DELETE',
    `/api/backups/trusted-signers/${encodeURIComponent(fingerprint)}`,
  )
}

export function createBackupStagingPlan(
  id: string,
  jwtSecretMode: JwtSecretMode,
): Promise<BackupStagingPlan> {
  return apiClient.request<BackupStagingPlan>(
    'POST',
    `/api/backups/${encodeURIComponent(id)}/staging-plans`,
    { jwtSecretMode },
  )
}

export function executeBackupStagingPlan(
  planId: string,
  planHash: string,
): Promise<BackupStagingPlan> {
  return apiClient.request<BackupStagingPlan>(
    'POST',
    `/api/backup-staging/${encodeURIComponent(planId)}/execute`,
    { planHash },
  )
}

export function inspectBackupStagingPlan(planId: string): Promise<BackupStagingPlan> {
  return apiClient.request<BackupStagingPlan>(
    'GET',
    `/api/backup-staging/${encodeURIComponent(planId)}`,
  )
}

export function abandonBackupStagingPlan(
  planId: string,
  planHash: string,
): Promise<void> {
  return apiClient.request<void>(
    'POST',
    `/api/backup-staging/${encodeURIComponent(planId)}/abandon`,
    { planHash },
  )
}

export function activateBackupStagingPlan(
  planId: string,
  planHash: string,
  activationId: string,
  resumeToken: string,
): Promise<BackupActivationStart> {
  return apiClient.request<BackupActivationStart>(
    'POST',
    `/api/backup-staging/${encodeURIComponent(planId)}/activate`,
    { planHash, activationId, resumeToken },
  )
}

export function getBackupActivation(
  activationId: string,
  resumeToken: string,
): Promise<BackupActivation> {
  return apiClient.request<BackupActivation>(
    'GET',
    `/api/backup-activations/${encodeURIComponent(activationId)}`,
    undefined,
    { headers: { 'X-PPBase-Activation-Token': resumeToken } },
  )
}

export function restoreBackupSdkCompatible(id: string): Promise<void> {
  return apiClient.request<void>('POST', `/api/backups/${encodeURIComponent(id)}/restore`)
}
