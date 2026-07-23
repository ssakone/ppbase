import type { BackupReadiness, BackupReadinessWarning } from '../api/types'

// Backup and restore run against PPBASE_DATABASE_URL and never require
// `ppbase init postgres` or `ppbase backup provision`. The API reports only
// operational blockers here (tools, local storage, and automatic restart);
// role-hardening guidance remains advisory.
export interface BackupReadinessView {
  warnings: BackupReadinessWarning[]
  createReady: boolean
  createMissing: string[]
  restoreReady: boolean
  restoreMissing: string[]
  toolsReady: boolean
  toolsMissing: string[]
  restartReady: boolean
  notes: string[]
}

export function buildBackupReadinessView(
  readiness: BackupReadiness,
): BackupReadinessView {
  const toolsReady = readiness.postgresqlTools?.configured ?? true
  const toolsMissing = readiness.postgresqlTools?.missing ?? []
  const restartReady = readiness.restart?.configured ?? true
  const createReady = readiness.create?.configured
    ?? !toolsMissing.some((tool) => tool === 'pg_dump' || tool === 'pg_restore')
  const restoreReady = readiness.restore?.configured
    ?? (restartReady && !toolsMissing.some((tool) => tool === 'pg_restore' || tool === 'psql'))
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
    toolsReady,
    toolsMissing,
    restartReady,
    notes,
  }
}
