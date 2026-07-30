import type { HealthStatus } from '../api/types'

export const BACKUP_DIALOG_AUTO_CLOSE_MS = 1500
export const BACKUP_STATUS_POLL_MS = 3500

export function isBackupOperationAvailable(health?: HealthStatus): boolean {
  return health?.data?.canBackup !== false
}
