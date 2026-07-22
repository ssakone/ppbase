import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  activationBlocksBackupOperations,
  activationShouldClearResume,
  activationStartMatchesResume,
  activationStatusMatchesResume,
  CANONICAL_STAGING_STATUSES,
  createActivationResume,
  isCanonicalStagingStatus,
  isExplicitHttpError,
  readActivationResume,
  readStagingResume,
  saveActivationResume,
  saveStagingResume,
} from '../../admin-ui/src/lib/backup-resume-state'

function installStorage(storage: Record<string, unknown>) {
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: { sessionStorage: storage },
  })
}

afterEach(() => {
  Reflect.deleteProperty(globalThis, 'window')
  vi.unstubAllGlobals()
})

describe('backup Dashboard resume state', () => {
  it('fails closed when sessionStorage access itself is denied', () => {
    Object.defineProperty(globalThis, 'window', {
      configurable: true,
      value: Object.defineProperty({}, 'sessionStorage', {
        get() {
          throw new DOMException('denied', 'SecurityError')
        },
      }),
    })

    expect(readActivationResume()).toBeNull()
    expect(readStagingResume()).toBeNull()
    expect(saveActivationResume(null)).toBe(false)
    expect(saveStagingResume(null)).toBe(false)
  })

  it('never propagates quota or cleanup failures', () => {
    installStorage({
      getItem: () => '{invalid-json',
      setItem: () => {
        throw new DOMException('quota', 'QuotaExceededError')
      },
      removeItem: () => {
        throw new DOMException('denied', 'SecurityError')
      },
    })

    expect(readActivationResume()).toBeNull()
    expect(readStagingResume()).toBeNull()
    expect(saveActivationResume({
      activationId: '0123456789abcdef0123456789abcdef',
      resumeToken: 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-',
      planId: 'plan-1',
      planHash: 'hash-1',
      mode: 'clone',
      backupId: 'backup-1',
      filename: 'backup.zip',
    })).toBe(false)
    expect(saveStagingResume({
      planId: 'plan-1',
      planHash: 'hash-1',
      backupId: 'backup-1',
      mode: 'disaster_recovery',
      filename: 'backup.zip',
    })).toBe(false)
  })

  it('round-trips only the minimal activation and staging fields', () => {
    const values = new Map<string, string>()
    installStorage({
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
      removeItem: (key: string) => values.delete(key),
    })

    const activation = {
      activationId: '0123456789abcdef0123456789abcdef',
      resumeToken: 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-',
      planId: 'plan-1',
      planHash: 'hash-1',
      mode: 'clone' as const,
      backupId: 'backup-1',
      filename: 'backup.zip',
    }
    const staging = {
      planId: 'plan-1',
      planHash: 'hash-1',
      backupId: 'backup-1',
      mode: 'disaster_recovery' as const,
      filename: 'backup.zip',
    }

    expect(saveActivationResume(activation)).toBe(true)
    expect(saveStagingResume(staging)).toBe(true)
    expect(readActivationResume()).toEqual(activation)
    expect(readStagingResume()).toEqual(staging)
    expect([...values.values()].join(' ')).not.toContain('database')
  })

  it('rejects weak or incomplete persisted activation credentials', () => {
    const values = new Map<string, string>([[
      'ppbase_backup_activation_resume_v1',
      JSON.stringify({
        activationId: 'activation-1',
        resumeToken: 'weak',
        planId: 'plan-1',
        planHash: 'hash-1',
        mode: 'clone',
        backupId: 'backup-1',
        filename: 'backup.zip',
      }),
    ]])
    installStorage({
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
      removeItem: (key: string) => values.delete(key),
    })

    expect(readActivationResume()).toBeNull()
    expect(values.has('ppbase_backup_activation_resume_v1')).toBe(false)
  })

  it('generates a client activation intent with strong protocol-shaped credentials', () => {
    const first = createActivationResume({
      planId: 'plan-1',
      planHash: 'hash-1',
      mode: 'clone',
      backupId: 'backup-1',
      filename: 'backup.zip',
    })
    const second = createActivationResume({
      planId: 'plan-1',
      planHash: 'hash-1',
      mode: 'clone',
      backupId: 'backup-1',
      filename: 'backup.zip',
    })

    expect(first.activationId).toMatch(/^[0-9a-f]{32}$/)
    expect(first.resumeToken).toMatch(/^[A-Za-z0-9_-]{64}$/)
    expect(second.activationId).not.toBe(first.activationId)
    expect(second.resumeToken).not.toBe(first.resumeToken)
  })

  it('recognizes every canonical staging state without treating unknown values as safe', () => {
    expect(CANONICAL_STAGING_STATUSES).toEqual([
      'planned',
      'running',
      'validated',
      'failed',
      'quarantined',
      'abandoned',
    ])
    for (const status of CANONICAL_STAGING_STATUSES) {
      expect(isCanonicalStagingStatus(status)).toBe(true)
    }
    expect(isCanonicalStagingStatus('succeeded')).toBe(false)
    expect(isCanonicalStagingStatus(undefined)).toBe(false)
  })

  it('distinguishes explicit HTTP rejection from an ambiguous transport failure', () => {
    expect(isExplicitHttpError({ status: 409, message: 'conflict' })).toBe(true)
    expect(isExplicitHttpError({ status: 500 })).toBe(true)
    expect(isExplicitHttpError({ status: 0 })).toBe(false)
    expect(isExplicitHttpError(new TypeError('fetch failed'))).toBe(false)
  })

  it('clears persisted activation tracking for every terminal status', () => {
    for (const status of ['succeeded', 'rolled_back', 'failed', 'action_required']) {
      expect(activationShouldClearResume(status)).toBe(true)
    }
    expect(activationShouldClearResume('starting')).toBe(false)
    expect(activationShouldClearResume(undefined)).toBe(false)
  })

  it('surfaces the canonical backup error message and code', async () => {
    Object.defineProperty(globalThis, 'window', {
      configurable: true,
      value: {
        location: { origin: 'http://ppbase.test' },
        sessionStorage: {
          getItem: () => null,
          setItem: () => undefined,
          removeItem: () => undefined,
        },
      },
    })
    const { getBackupErrorMessage } = await import(
      '../../admin-ui/src/api/endpoints/backups'
    )

    expect(getBackupErrorMessage({
      detail: {
        message: 'The target root is unsafe.',
        data: { code: 'unsafe_backup_root' },
      },
    }, 'Unavailable')).toBe('The target root is unsafe. (unsafe_backup_root)')
  })

  it('requires activation POST and polling responses to match the saved intent', () => {
    const resume = {
      activationId: '0123456789abcdef0123456789abcdef',
      resumeToken: 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-',
      planId: 'plan-1',
      planHash: 'hash-1',
      mode: 'disaster_recovery' as const,
      backupId: 'backup-1',
      filename: 'backup.zip',
    }

    expect(activationStartMatchesResume({
      activationId: resume.activationId,
      resumeToken: resume.resumeToken,
      planId: resume.planId,
      planHash: resume.planHash,
      backupId: resume.backupId,
      jwtSecretMode: resume.mode,
      status: 'restart_scheduled',
    }, resume)).toBe(true)
    expect(activationStartMatchesResume({
      activationId: 'ffffffffffffffffffffffffffffffff',
      resumeToken: resume.resumeToken,
      planId: resume.planId,
      planHash: resume.planHash,
      backupId: resume.backupId,
      jwtSecretMode: resume.mode,
      status: 'restart_scheduled',
    }, resume)).toBe(false)
    expect(activationStartMatchesResume({
      activationId: resume.activationId,
      resumeToken: resume.resumeToken,
      planId: 'plan-2',
      planHash: resume.planHash,
      backupId: resume.backupId,
      jwtSecretMode: resume.mode,
      status: 'restart_scheduled',
    }, resume)).toBe(false)
    expect(activationStatusMatchesResume({
      activationId: resume.activationId,
      planId: resume.planId,
      planHash: resume.planHash,
      backupId: resume.backupId,
      jwtSecretMode: resume.mode,
      status: 'succeeded',
    }, resume)).toBe(true)
    expect(activationStatusMatchesResume({
      activationId: 'ffffffffffffffffffffffffffffffff',
      planId: resume.planId,
      planHash: resume.planHash,
      backupId: resume.backupId,
      jwtSecretMode: resume.mode,
      status: 'succeeded',
    }, resume)).toBe(false)

    expect(activationBlocksBackupOperations(resume)).toBe(true)
    expect(activationBlocksBackupOperations(resume, {
      activationId: resume.activationId,
      planId: resume.planId,
      planHash: resume.planHash,
      backupId: resume.backupId,
      jwtSecretMode: resume.mode,
      status: 'health_check',
    })).toBe(true)
    expect(activationBlocksBackupOperations(resume, {
      activationId: resume.activationId,
      planId: resume.planId,
      planHash: resume.planHash,
      backupId: resume.backupId,
      jwtSecretMode: resume.mode,
      status: 'rolled_back',
    })).toBe(false)
    expect(activationBlocksBackupOperations(null)).toBe(false)
  })

  it('sends pre-generated activation credentials and descriptor-safe abandon intent', async () => {
    const requests: Array<{ path: string; body: Record<string, unknown> }> = []
    installStorage({
      getItem: () => null,
      setItem: () => undefined,
      removeItem: () => undefined,
    })
    Object.assign((globalThis as { window: Record<string, unknown> }).window, {
      location: { origin: 'http://ppbase.test' },
    })
    vi.stubGlobal('localStorage', {
      getItem: () => null,
      setItem: () => undefined,
      removeItem: () => undefined,
    })
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
      requests.push({
        path: url,
        body: JSON.parse(String(init.body || '{}')) as Record<string, unknown>,
      })
      if (url.endsWith('/abandon')) return new Response(null, { status: 204 })
      return new Response(JSON.stringify({
        activationId: '0123456789abcdef0123456789abcdef',
        resumeToken: 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-',
        planId: 'plan-1',
        planHash: 'hash-1',
        backupId: 'backup-1',
        jwtSecretMode: 'clone',
        status: 'restart_scheduled',
      }), { status: 202 })
    })

    const {
      abandonBackupStagingPlan,
      activateBackupStagingPlan,
    } = await import('../../admin-ui/src/api/endpoints/backups')
    await activateBackupStagingPlan(
      'plan-1',
      'hash-1',
      '0123456789abcdef0123456789abcdef',
      'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-',
    )
    await abandonBackupStagingPlan('plan-1', 'hash-1')

    expect(requests).toEqual([
      {
        path: 'http://ppbase.test/api/backup-staging/plan-1/activate',
        body: {
          planHash: 'hash-1',
          activationId: '0123456789abcdef0123456789abcdef',
          resumeToken: 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-',
        },
      },
      {
        path: 'http://ppbase.test/api/backup-staging/plan-1/abandon',
        body: { planHash: 'hash-1' },
      },
    ])
  })
})
