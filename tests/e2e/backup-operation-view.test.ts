import { describe, expect, it } from 'vitest'

import type { HealthStatus } from '../../admin-ui/src/api/types'
import {
  BACKUP_DIALOG_AUTO_CLOSE_MS,
  BACKUP_STATUS_POLL_MS,
  isBackupOperationAvailable,
} from '../../admin-ui/src/lib/backup-operation-view'

function health(canBackup?: boolean): HealthStatus {
  return {
    code: 200,
    message: 'API is healthy.',
    data: { canBackup },
  }
}

describe('PocketBase-like backup operation presentation', () => {
  it('keeps the PocketBase dialog and health polling timings', () => {
    expect(BACKUP_DIALOG_AUTO_CLOSE_MS).toBe(1500)
    expect(BACKUP_STATUS_POLL_MS).toBe(3500)
  })

  it('blocks backup mutations only when the server reports an active operation', () => {
    expect(isBackupOperationAvailable(health(false))).toBe(false)
    expect(isBackupOperationAvailable(health(true))).toBe(true)
    expect(isBackupOperationAvailable(health())).toBe(true)
    expect(isBackupOperationAvailable()).toBe(true)
  })
})
