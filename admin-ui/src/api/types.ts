export interface Collection {
  id: string
  name: string
  type: 'base' | 'auth' | 'view'
  system?: boolean
  schema?: Field[]
  fields?: Field[]
  listRule: string | null
  viewRule: string | null
  createRule: string | null
  updateRule: string | null
  deleteRule: string | null
  options?: { [key: string]: unknown }
  created: string
  updated: string
}

export interface Field {
  name: string
  type: string
  required?: boolean
  options?: FieldOptions
  // Flat format fields (PocketBase v0.23+)
  min?: number
  max?: number
  pattern?: string
  onlyInt?: boolean
  values?: string[]
  maxSelect?: number
  collectionId?: string
  cascadeDelete?: boolean
  maxSize?: number
  mimeTypes?: string[]
  onlyDomains?: string[]
  exceptDomains?: string[]
  convertURLs?: boolean
}

export interface FieldOptions {
  min?: number
  max?: number
  pattern?: string
  onlyInt?: boolean
  values?: string[]
  maxSelect?: number
  collectionId?: string
  cascadeDelete?: boolean
  maxSize?: number
  mimeTypes?: string[]
  onlyDomains?: string[]
  exceptDomains?: string[]
  convertURLs?: boolean
  query?: string
}

export interface RecordModel {
  id: string
  collectionId?: string
  collectionName?: string
  created: string
  updated: string
  expand?: Record<string, unknown>
  [key: string]: unknown
}

export interface PaginatedResult<T> {
  page: number
  perPage: number
  totalItems: number
  totalPages: number
  items: T[]
}

export interface AdminAuth {
  token: string
  admin: { id: string; email: string }
}

export interface Settings {
  meta?: { appName?: string; appUrl?: string }
  [key: string]: unknown
}

export interface Migration {
  file: string
  status: 'applied' | 'pending'
  applied: string | null
}

export interface MigrationStatus {
  total: number
  applied: number
  pending: number
  lastApplied: string | null
}

export interface DatabaseTable {
  name: string
  columns: { name: string; type: string }[]
}

export interface ApiError {
  status: number
  message: string
  data: { [key: string]: unknown }
}

export interface LogRecord {
  id: string
  url: string
  method: string
  status: number
  execTime: number
  remoteIp: string
  referer?: string
  userAgent?: string
  requestBody?: Record<string, unknown> | string | null
  responseBody?: Record<string, unknown> | string | null
  meta?: Record<string, unknown>
  created: string
  updated: string
}

export interface LogStats {
  total: number
  errors: number
  avgMs: number
  uniqueIps: number
}

export interface HealthStatus {
  code: number
  message: string
  data: {
    version?: string
    [key: string]: unknown
  }
}

export type BackupTrustStatus =
  | 'trusted_local'
  | 'trusted_external'
  | 'authenticated_untrusted'
  | 'revoked'
  | 'invalid'
  | 'unknown'

export type BackupIntegrityStatus = 'valid' | 'invalid' | 'unchecked' | string

export interface BackupListItem {
  id: string
  key: string
  filename?: string | null
  size?: number | null
  totalSize?: number | null
  createdAt?: string | null
  modified?: string | null
  status?: string
  integrityStatus?: BackupIntegrityStatus
  authenticated?: boolean
  trustStatus?: BackupTrustStatus
  signerFingerprintSha256?: string | null
  resourceCount?: number | null
  errorCode?: string | null
}

export interface BackupResource {
  path: string
  size: number
  sha256: string
}

export interface BackupInspection extends BackupListItem {
  signerPublicKey?: string
  resourcesVerified?: boolean
  metadata?: Record<string, unknown>
  resources?: BackupResource[]
  resourceOffset?: number
  resourceLimit?: number
  resourcesReturned?: number
  hasMoreResources?: boolean
}

export interface BackupIdentity {
  algorithm: 'Ed25519' | string
  publicKey: string
  fingerprintSha256: string
}

export interface BackupReadinessCheck {
  configured: boolean
  missing: string[]
}

export interface BackupReadinessWarning {
  code: string
  name: string
  detail: string
}

export interface BackupReadiness {
  create: BackupReadinessCheck
  restore: BackupReadinessCheck
  activation: BackupReadinessCheck
  postgresqlTools: BackupReadinessCheck
  storage: BackupReadinessCheck
  controlPlane: BackupReadinessCheck
  storageBackend: string
  warnings?: BackupReadinessWarning[]
  onboarding: {
    recommended: 'production'
    productionCommand: string
    localCommand: string
    doctorCommand: string
  }
}

export interface BackupTrustEntry {
  fingerprintSha256: string
  publicKey?: string
  label?: string | null
  approvedAt?: string | null
  approvedBy?: string | null
  actorId?: string | null
  status?: string
  trustStatus?: string
}

export type JwtSecretMode = 'disaster_recovery' | 'clone'

export interface BackupStagingPlan {
  id: string
  backupId: string
  manifestSha256: string
  destinationFingerprintSha256?: string
  targetDatabase?: string
  targetDataDir?: string
  jwtSecretMode: JwtSecretMode
  createdAt?: string
  actorId?: string | null
  planHash: string
  status: string
  preflightWarnings?: string[]
  validation?: Record<string, unknown>
  migrationsApplied?: string[]
  cloneRotation?: Record<string, unknown>
  failureCode?: string
  activationPerformed?: boolean
  [key: string]: unknown
}

export interface BackupActivationStart {
  activationId: string
  resumeToken: string
  planId: string
  planHash: string
  backupId: string
  jwtSecretMode: JwtSecretMode
  status: string
  phase?: string
}

export interface BackupActivation {
  id?: string
  activationId?: string
  backupId?: string
  planId?: string
  planHash?: string
  jwtSecretMode?: JwtSecretMode
  status: string
  phase?: string
  message?: string
  startedAt?: string
  updatedAt?: string
  completedAt?: string
  health?: Record<string, unknown>
  rollback?: Record<string, unknown>
  errorCode?: string
  failureCode?: string
  actionRequired?: string
  [key: string]: unknown
}

export interface OAuth2ProviderConfig {
  name: string
  clientId: string
  clientSecret: string
}

export interface CollectionOAuth2Options {
  enabled: boolean
  mappedFields: {
    id?: string
    name?: string
    username?: string
    avatarURL?: string
  }
  providers: OAuth2ProviderConfig[]
}

export interface HookFunction {
  hookType: string
  bindingId: string
  handlerName: string
}

export interface HookState {
  id: string
  filename: string
  filepath: string
  name: string
  description: string
  status: 'loaded' | 'error' | 'disabled' | 'unsupported_for_hot_reload'
  enabled: boolean
  functions: HookFunction[]
  error: string | null
  errorTraceback: string | null
  lastLoaded: string | null
  hasRoutes: boolean
  hasHttpMiddleware: boolean
  restartRequired: boolean
}

export interface HooksListResult {
  items: HookState[]
  hooksDir: string
  runtime: HooksRuntime
}

export interface HooksRescanResult {
  reloaded: string[]
  items: HookState[]
}

export interface HooksRuntime {
  disabled: string[]
  autoRestartOnChange: boolean
  canRestart: boolean
  restartPending: boolean
}
