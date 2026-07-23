import { describe, expect, it } from 'vitest'

import type { BackupReadiness } from '../../admin-ui/src/api/types'
import { buildBackupReadinessView } from '../../admin-ui/src/lib/backup-readiness-view'
import { isBackupTrusted } from '../../admin-ui/src/lib/backup-trust'

function configuredReadiness(): BackupReadiness {
  const configured = { configured: true, missing: [] }
  return {
    create: { ...configured },
    restore: { ...configured },
    restart: { ...configured },
    postgresqlTools: { ...configured },
    storage: { ...configured },
    controlPlane: { ...configured },
    storageBackend: 'local',
    warnings: [],
    onboarding: {
      recommended: 'automatic',
      productionCommand: 'ppbase backup doctor',
      localCommand: 'ppbase backup doctor',
      doctorCommand: 'ppbase backup doctor',
    },
  }
}

describe('backup readiness presentation', () => {
  it('allows both operations when their concrete prerequisites are ready', () => {
    const view = buildBackupReadinessView(configuredReadiness())

    expect(view.createReady).toBe(true)
    expect(view.restoreReady).toBe(true)
    expect(view.createMissing).toEqual([])
    expect(view.restoreMissing).toEqual([])
    expect(view.warnings).toEqual([])
    expect(view.notes).toEqual([])
  })

  it('keeps runtime-role hardening warnings non-blocking', () => {
    const readiness = configuredReadiness()
    readiness.warnings = [{
      code: 'runtime_superuser',
      name: 'runtime_role',
      detail: 'Backup and restore use the active PostgreSQL superuser runtime.',
    }]

    const view = buildBackupReadinessView(readiness)

    expect(view.warnings).toEqual(readiness.warnings)
    expect(view.createReady).toBe(true)
    expect(view.restoreReady).toBe(true)
  })

  it('keeps creation available when only restore prerequisites are missing', () => {
    const readiness = configuredReadiness()
    readiness.restore = {
      configured: false,
      missing: ['psql', 'PPBASE_RESTART_CMD'],
    }
    readiness.restart = { configured: false, missing: ['PPBASE_RESTART_CMD'] }
    readiness.postgresqlTools = { configured: false, missing: ['psql'] }

    const view = buildBackupReadinessView(readiness)

    expect(view.createReady).toBe(true)
    expect(view.restoreReady).toBe(false)
    expect(view.restoreMissing).toEqual(['psql', 'PPBASE_RESTART_CMD'])
    expect(view.restartReady).toBe(false)
  })
})

describe('backup restore trust gate', () => {
  it('accepts only explicit trusted statuses when a status is present', () => {
    expect(isBackupTrusted('trusted_local', false)).toBe(true)
    expect(isBackupTrusted('trusted_external', false)).toBe(true)

    for (const status of [
      'authenticated_untrusted',
      'revoked',
      'invalid',
      'unknown',
      'future_status',
      '',
    ]) {
      expect(isBackupTrusted(status, true)).toBe(false)
    }
  })

  it('uses authenticated only as a fallback for responses without trustStatus', () => {
    expect(isBackupTrusted(undefined, true)).toBe(true)
    expect(isBackupTrusted(null, true)).toBe(true)
    expect(isBackupTrusted(undefined, false)).toBe(false)
  })
})
