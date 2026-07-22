import type {
  BackupReadiness,
  BackupReadinessCheck,
  BackupReadinessWarning,
} from '../api/types'

export const BACKUP_READINESS_SUCCESS_TEXT = 'All set for backup & restore'

type BackupReadinessCheckKey =
  | 'create'
  | 'restore'
  | 'activation'
  | 'postgresqlTools'
  | 'storage'
  | 'controlPlane'

const CHECKS: ReadonlyArray<{
  key: BackupReadinessCheckKey
  label: string
}> = [
  { key: 'create', label: 'Backup creation' },
  { key: 'restore', label: 'Restore staging' },
  { key: 'activation', label: 'Activation restart' },
  { key: 'postgresqlTools', label: 'PostgreSQL tools' },
  { key: 'storage', label: 'Storage' },
  { key: 'controlPlane', label: 'Control plane and target root' },
]

export interface BackupReadinessBlocker {
  action: string
  areas: string[]
}

export interface BackupReadinessDetail {
  key: BackupReadinessCheckKey
  label: string
  check: BackupReadinessCheck
}

export interface BackupReadinessView {
  ready: boolean
  successText: typeof BACKUP_READINESS_SUCCESS_TEXT
  warnings: BackupReadinessWarning[]
  blockers: BackupReadinessBlocker[]
  details: BackupReadinessDetail[]
  createBlocked: boolean
  restoreBlocked: boolean
}

export function buildBackupReadinessView(
  readiness: BackupReadiness,
): BackupReadinessView {
  const blockerAreas = new Map<string, string[]>()
  const details = CHECKS.map(({ key, label }) => {
    const check = readiness[key]
    if (!check.configured) {
      const actions = check.missing.length > 0
        ? check.missing
        : [`Review ${label.toLowerCase()} configuration`]
      for (const action of actions) {
        const areas = blockerAreas.get(action) || []
        if (!areas.includes(label)) areas.push(label)
        blockerAreas.set(action, areas)
      }
    }
    return { key, label, check }
  })

  return {
    ready: details.every(({ check }) => check.configured),
    successText: BACKUP_READINESS_SUCCESS_TEXT,
    warnings: [...(readiness.warnings || [])],
    blockers: [...blockerAreas].map(([action, areas]) => ({ action, areas })),
    details,
    createBlocked: !readiness.create.configured,
    restoreBlocked: !readiness.restore.configured,
  }
}
