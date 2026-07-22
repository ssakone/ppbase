import { describe, expect, it } from 'vitest'

import type { BackupReadiness } from '../../admin-ui/src/api/types'
import {
  BACKUP_READINESS_SUCCESS_TEXT,
  buildBackupReadinessView,
} from '../../admin-ui/src/lib/backup-readiness-view'

function configuredReadiness(): BackupReadiness {
  const configured = { configured: true, missing: [] }
  return {
    create: { ...configured },
    restore: { ...configured },
    activation: { ...configured },
    postgresqlTools: { ...configured },
    storage: { ...configured },
    controlPlane: { ...configured },
    storageBackend: 'local',
    warnings: [],
    onboarding: {
      recommended: 'production',
      productionCommand: 'ppbase backup provision --plan',
      localCommand: 'ppbase backup provision --plan --local',
      doctorCommand: 'ppbase backup doctor',
    },
  }
}

describe('compact backup readiness presentation', () => {
  it('reduces a fully configured deployment to the single success state', () => {
    const view = buildBackupReadinessView(configuredReadiness())

    expect(view.ready).toBe(true)
    expect(view.successText).toBe('All set for backup & restore')
    expect(view.successText).toBe(BACKUP_READINESS_SUCCESS_TEXT)
    expect(view.blockers).toEqual([])
    expect(view.warnings).toEqual([])
    expect(view.createBlocked).toBe(false)
    expect(view.restoreBlocked).toBe(false)
  })

  it('keeps the legacy runtime superuser warning non-blocking', () => {
    const readiness = configuredReadiness()
    readiness.warnings = [{
      code: 'legacy_runtime_superuser',
      name: 'runtime_role',
      detail: 'PostgreSQL superuser runtime',
    }]

    const view = buildBackupReadinessView(readiness)

    expect(view.ready).toBe(true)
    expect(view.warnings).toEqual(readiness.warnings)
    expect(view.createBlocked).toBe(false)
    expect(view.restoreBlocked).toBe(false)
  })

  it('shows real blockers as deduplicated actions and retains technical details', () => {
    const readiness = configuredReadiness()
    readiness.create = {
      configured: false,
      missing: ['PPBASE_BACKUP_DUMP_DATABASE_URL'],
    }
    readiness.storage = {
      configured: false,
      missing: ['local business-file storage backend'],
    }
    readiness.controlPlane = {
      configured: false,
      missing: ['local business-file storage backend'],
    }

    const view = buildBackupReadinessView(readiness)

    expect(view.ready).toBe(false)
    expect(view.createBlocked).toBe(true)
    expect(view.restoreBlocked).toBe(false)
    expect(view.blockers).toEqual([
      {
        action: 'PPBASE_BACKUP_DUMP_DATABASE_URL',
        areas: ['Backup creation'],
      },
      {
        action: 'local business-file storage backend',
        areas: ['Storage', 'Control plane and target root'],
      },
    ])
    expect(view.details).toHaveLength(6)
  })
})
