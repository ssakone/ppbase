import type { BackupReadiness, BackupReadinessWarning } from '../api/types'

// Backup and restore run against PPBASE_DATABASE_URL. The native engine speaks
// the PostgreSQL wire protocol directly, so there are no external client tools
// to check; the API reports only operational blockers here (local storage and
// automatic restart). Role-hardening guidance remains advisory.
export interface BackupReadinessView {
  warnings: BackupReadinessWarning[]
  createReady: boolean
  createMissing: string[]
  restoreReady: boolean
  restoreMissing: string[]
  restartReady: boolean
  notes: string[]
}

export function buildBackupReadinessView(
  readiness: BackupReadiness,
): BackupReadinessView {
  const restartReady = readiness.restart?.configured ?? true
  const createReady = readiness.create?.configured ?? true
  const restoreReady = readiness.restore?.configured ?? restartReady
  const createMissing = readiness.create?.missing ?? []
  const restoreMissing = readiness.restore?.missing ?? []
  const storageBackend = (readiness.storageBackend || 'local').toLowerCase()

  const notes: string[] = []
  if (storageBackend !== 'local') {
    notes.push(
      'Native backups capture local file storage; the configured storage '
      + 'backend is not local, so external object storage is not included.',
    )
  }

  return {
    warnings: [...(readiness.warnings || [])],
    createReady,
    createMissing,
    restoreReady,
    restoreMissing,
    restartReady,
    notes,
  }
}
