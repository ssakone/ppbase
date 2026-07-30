import { describe, expect, it } from 'vitest'

import type { BackupReadiness } from '../../admin-ui/src/api/types'
import { buildBackupReadinessView } from '../../admin-ui/src/lib/backup-readiness-view'

function configuredReadiness(): BackupReadiness {
  const configured = { configured: true, missing: [] }
  return {
    create: { ...configured },
    restore: { ...configured },
    restart: { ...configured },
    storage: { ...configured },
    storageBackend: 'local',
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
    expect(view.notes).toEqual([])
  })

  it('keeps creation available when only restore prerequisites are missing', () => {
    const readiness = configuredReadiness()
    readiness.restore = {
      configured: false,
      missing: ['PPBASE_RESTART_CMD'],
    }
    readiness.restart = { configured: false, missing: ['PPBASE_RESTART_CMD'] }

    const view = buildBackupReadinessView(readiness)

    expect(view.createReady).toBe(true)
    expect(view.restoreReady).toBe(false)
    expect(view.restoreMissing).toEqual(['PPBASE_RESTART_CMD'])
    expect(view.restartReady).toBe(false)
  })
})
