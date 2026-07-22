import type {
  BackupActivation,
  BackupActivationStart,
  JwtSecretMode,
} from '@/api/types'

export interface ActivationResumeState {
  activationId: string
  resumeToken: string
  planId: string
  planHash: string
  mode: JwtSecretMode
  backupId: string
  filename: string
}

export interface StagingResumeState {
  planId: string
  planHash: string
  backupId: string
  mode: JwtSecretMode
  filename: string
}

const ACTIVATION_STORAGE_KEY = 'ppbase_backup_activation_resume_v1'
const STAGING_STORAGE_KEY = 'ppbase_backup_staging_resume_v1'
const ACTIVATION_ID_PATTERN = /^[0-9a-f]{32}$/
const RESUME_TOKEN_PATTERN = /^[A-Za-z0-9_-]{43,512}$/

export const CANONICAL_STAGING_STATUSES = [
  'planned',
  'running',
  'validated',
  'failed',
  'quarantined',
  'abandoned',
] as const

export type CanonicalStagingStatus = typeof CANONICAL_STAGING_STATUSES[number]

interface ActivationResumeInput {
  planId: string
  planHash: string
  mode: JwtSecretMode
  backupId: string
  filename: string
}

function isMode(value: unknown): value is JwtSecretMode {
  return value === 'clone' || value === 'disaster_recovery'
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0
}

export function isCanonicalStagingStatus(value: unknown): value is CanonicalStagingStatus {
  return typeof value === 'string'
    && (CANONICAL_STAGING_STATUSES as readonly string[]).includes(value)
}

export function isExplicitHttpError(error: unknown): boolean {
  if (error === null || typeof error !== 'object') return false
  const status = (error as { status?: unknown }).status
  return typeof status === 'number'
    && Number.isInteger(status)
    && status >= 100
    && status <= 599
}

function randomBytes(length: number): Uint8Array {
  const crypto = globalThis.crypto
  if (!crypto || typeof crypto.getRandomValues !== 'function') {
    throw new Error('Secure browser randomness is unavailable.')
  }
  return crypto.getRandomValues(new Uint8Array(length))
}

function bytesToHex(bytes: Uint8Array): string {
  return Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('')
}

function bytesToBase64Url(bytes: Uint8Array): string {
  let binary = ''
  for (const value of bytes) binary += String.fromCharCode(value)
  return globalThis.btoa(binary)
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/g, '')
}

export function createActivationResume(input: ActivationResumeInput): ActivationResumeState {
  return {
    activationId: bytesToHex(randomBytes(16)),
    resumeToken: bytesToBase64Url(randomBytes(48)),
    planId: input.planId,
    planHash: input.planHash,
    mode: input.mode,
    backupId: input.backupId,
    filename: input.filename,
  }
}

export function activationStartMatchesResume(
  started: BackupActivationStart,
  resume: ActivationResumeState,
): boolean {
  return started.activationId === resume.activationId
    && started.resumeToken === resume.resumeToken
    && started.planId === resume.planId
    && started.planHash === resume.planHash
    && started.backupId === resume.backupId
    && started.jwtSecretMode === resume.mode
}

export function activationStatusMatchesResume(
  activation: BackupActivation,
  resume: ActivationResumeState,
): boolean {
  const activationId = activation.activationId || activation.id
  return activationId === resume.activationId
    && activation.planId === resume.planId
    && activation.planHash === resume.planHash
    && activation.backupId === resume.backupId
    && activation.jwtSecretMode === resume.mode
}

export function activationBlocksBackupOperations(
  resume: ActivationResumeState | null,
  activation?: BackupActivation,
): boolean {
  if (!resume) return false
  if (!activation || !activationStatusMatchesResume(activation, resume)) return true
  return !['succeeded', 'rolled_back', 'failed', 'action_required'].includes(
    activation.status,
  )
}

export function activationShouldClearResume(status?: string): boolean {
  return ['succeeded', 'rolled_back', 'failed', 'action_required'].includes(status || '')
}

function browserSessionStorage(): Storage | null {
  if (typeof window === 'undefined') return null
  try {
    return window.sessionStorage
  } catch {
    return null
  }
}

function removeStoredValue(key: string): boolean {
  const storage = browserSessionStorage()
  if (!storage) return false
  try {
    storage.removeItem(key)
    return true
  } catch {
    return false
  }
}

function readStoredObject(key: string): Record<string, unknown> | null {
  const storage = browserSessionStorage()
  if (!storage) return null
  let raw: string | null
  try {
    raw = storage.getItem(key)
  } catch {
    return null
  }
  if (!raw) return null
  try {
    const parsed: unknown = JSON.parse(raw)
    if (parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>
    }
  } catch {
    removeStoredValue(key)
    return null
  }
  removeStoredValue(key)
  return null
}

function writeStoredValue(key: string, value: Record<string, string> | null): boolean {
  const storage = browserSessionStorage()
  if (!storage) return false
  try {
    if (value) storage.setItem(key, JSON.stringify(value))
    else storage.removeItem(key)
    return true
  } catch {
    return false
  }
}

export function readActivationResume(): ActivationResumeState | null {
  const parsed = readStoredObject(ACTIVATION_STORAGE_KEY)
  if (!parsed) return null
  if (
    !isNonEmptyString(parsed.activationId)
    || !ACTIVATION_ID_PATTERN.test(parsed.activationId)
    || !isNonEmptyString(parsed.resumeToken)
    || !RESUME_TOKEN_PATTERN.test(parsed.resumeToken)
    || !isNonEmptyString(parsed.planId)
    || !isNonEmptyString(parsed.planHash)
    || !isNonEmptyString(parsed.backupId)
    || !isNonEmptyString(parsed.filename)
    || !isMode(parsed.mode)
  ) {
    removeStoredValue(ACTIVATION_STORAGE_KEY)
    return null
  }
  return {
    activationId: parsed.activationId,
    resumeToken: parsed.resumeToken,
    planId: parsed.planId,
    planHash: parsed.planHash,
    mode: parsed.mode,
    backupId: parsed.backupId,
    filename: parsed.filename,
  }
}

export function saveActivationResume(value: ActivationResumeState | null): boolean {
  return writeStoredValue(
    ACTIVATION_STORAGE_KEY,
    value
      ? {
        activationId: value.activationId,
        resumeToken: value.resumeToken,
        planId: value.planId,
        planHash: value.planHash,
        mode: value.mode,
        backupId: value.backupId,
        filename: value.filename,
      }
      : null,
  )
}

export function readStagingResume(): StagingResumeState | null {
  const parsed = readStoredObject(STAGING_STORAGE_KEY)
  if (!parsed) return null
  if (
    !isNonEmptyString(parsed.planId)
    || !isNonEmptyString(parsed.planHash)
    || !isNonEmptyString(parsed.backupId)
    || !isNonEmptyString(parsed.filename)
    || !isMode(parsed.mode)
  ) {
    removeStoredValue(STAGING_STORAGE_KEY)
    return null
  }
  return {
    planId: parsed.planId,
    planHash: parsed.planHash,
    backupId: parsed.backupId,
    mode: parsed.mode,
    filename: parsed.filename,
  }
}

export function saveStagingResume(value: StagingResumeState | null): boolean {
  return writeStoredValue(
    STAGING_STORAGE_KEY,
    value
      ? {
        planId: value.planId,
        planHash: value.planHash,
        backupId: value.backupId,
        mode: value.mode,
        filename: value.filename,
      }
      : null,
  )
}
