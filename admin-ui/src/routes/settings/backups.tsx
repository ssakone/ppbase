import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ArchiveRestore,
  CheckCircle2,
  Clock3,
  Download,
  Eye,
  FileArchive,
  Fingerprint,
  Plus,
  RefreshCw,
  RotateCcw,
  ShieldAlert,
  ShieldCheck,
  Trash2,
  Upload,
  XCircle,
} from 'lucide-react'
import { toast } from 'sonner'
import type { UploadProgress } from '@/api/client'
import type {
  BackupInspection,
  BackupListItem,
  BackupStagingPlan,
  BackupTrustEntry,
} from '@/api/types'
import {
  backupKey,
  downloadBackup,
  getBackupErrorMessage,
} from '@/api/endpoints/backups'
import {
  useAbandonBackupStagingPlan,
  useApproveBackupSigner,
  useBackup,
  useBackupActivation,
  useBackupIdentity,
  useBackupReadiness,
  useBackups,
  useBackupTrust,
  useBackupStagingPlan,
  useCreateBackup,
  useDeleteBackup,
  useRevokeBackupSigner,
  useUploadBackup,
} from '@/hooks/use-backups'
import { useSettings, useUpdateSettings } from '@/hooks/use-settings'
import { useAuth } from '@/context/auth-context'
import { ContentHeader } from '@/components/content-header'
import { Breadcrumb } from '@/components/breadcrumb'
import { ConfirmDialog } from '@/components/confirm-dialog'
import { EmptyState } from '@/components/empty-state'
import { LoadingSpinner } from '@/components/loading-spinner'
import { BackupRestoreWizard } from '@/components/backups/backup-restore-wizard'
import {
  activationBlocksBackupOperations,
  activationShouldClearResume,
  activationStatusMatchesResume,
  isCanonicalStagingStatus,
  readActivationResume,
  readStagingResume,
  saveActivationResume,
  saveStagingResume,
  type ActivationResumeState,
  type StagingResumeState,
} from '@/lib/backup-resume-state'
import { buildBackupReadinessView } from '@/lib/backup-readiness-view'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'

const BACKUP_RESOURCE_PAGE_SIZE = 50

function formatBytes(value?: number | null): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  if (value === 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const exponent = Math.min(units.length - 1, Math.floor(Math.log(value) / Math.log(1024)))
  const amount = value / (1024 ** exponent)
  return `${amount >= 10 || exponent === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[exponent]}`
}

function formatDate(value?: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function shortFingerprint(value?: string | null): string {
  if (!value) return '—'
  return value.length > 24 ? `${value.slice(0, 12)}…${value.slice(-12)}` : value
}

function displayFilename(backup: BackupListItem): string {
  return backup.filename || backup.key || backup.id
}

function isUntrusted(backup?: BackupListItem | BackupInspection): boolean {
  return ['authenticated_untrusted', 'unknown', 'revoked', 'invalid'].includes(
    backup?.trustStatus || '',
  )
}

function errorStatus(error: unknown): number | null {
  if (!error || typeof error !== 'object') return null
  const status = (error as { status?: unknown }).status
  return typeof status === 'number' ? status : null
}

function stagingPlanMatchesResume(
  plan: BackupStagingPlan,
  resume: StagingResumeState,
): boolean {
  return plan.id === resume.planId
    && plan.planHash === resume.planHash
    && plan.backupId === resume.backupId
    && plan.jwtSecretMode === resume.mode
}

function StatusBadge({ backup }: { backup: BackupListItem }) {
  if (backup.status === 'invalid' || backup.integrityStatus === 'invalid') {
    return <Badge variant="destructive">Invalid</Badge>
  }
  if (isUntrusted(backup) || backup.status === 'quarantined') {
    return <Badge className="border-amber-200 bg-amber-100 text-amber-800">Quarantined</Badge>
  }
  return <Badge className="border-emerald-200 bg-emerald-100 text-emerald-800">Sealed</Badge>
}

function TrustBadge({ backup }: { backup: BackupListItem }) {
  if (backup.trustStatus === 'trusted_local') return <Badge variant="secondary">Local</Badge>
  if (backup.trustStatus === 'trusted_external') {
    return <Badge className="border-indigo-200 bg-indigo-100 text-indigo-800">Approved external</Badge>
  }
  if (backup.trustStatus === 'invalid') return <Badge variant="destructive">Invalid signature</Badge>
  if (isUntrusted(backup)) return <Badge variant="destructive">Untrusted signer</Badge>
  return <Badge variant="outline">Unknown</Badge>
}

function CreateBackupDialog({
  open,
  disabled,
  onOpenChange,
}: {
  open: boolean
  disabled: boolean
  onOpenChange: (open: boolean) => void
}) {
  const createBackup = useCreateBackup()
  const [name, setName] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    if (open) {
      setName('')
      setError('')
    }
  }, [open])

  const validName = !name || (name.endsWith('.zip') && !/[\\/\0]/.test(name))

  const submit = async () => {
    if (!validName || disabled) return
    setError('')
    try {
      await createBackup.mutateAsync(name.trim() || undefined)
      toast.success('Backup created and sealed successfully.')
      onOpenChange(false)
    } catch (err) {
      setError(getBackupErrorMessage(err, 'The backup could not be created.'))
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !createBackup.isPending && onOpenChange(next)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Create backup</DialogTitle>
          <DialogDescription>
            PPBase temporarily blocks writes while PostgreSQL and local files are captured and signed.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div>
            <Label htmlFor="backup-name">ZIP filename</Label>
            <Input
              id="backup-name"
              className="mt-2"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="Leave empty to generate a name"
              disabled={disabled || createBackup.isPending}
            />
            <p className="mt-1.5 text-xs text-slate-500">Optional. Must be a safe basename ending in .zip.</p>
          </div>
          {!validName && <p className="text-sm text-red-600">Use a basename ending in .zip without path separators.</p>}
          {error && <p className="rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={createBackup.isPending}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={disabled || !validName || createBackup.isPending}>
            {createBackup.isPending && <LoadingSpinner size="sm" />}
            Create backup
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function UploadBackupDialog({
  open,
  disabled,
  onOpenChange,
  onUploaded,
}: {
  open: boolean
  disabled: boolean
  onOpenChange: (open: boolean) => void
  onUploaded: (inspection: BackupInspection) => void
}) {
  const uploadBackup = useUploadBackup()
  const abortRef = useRef<AbortController | null>(null)
  const [file, setFile] = useState<File | null>(null)
  const [progress, setProgress] = useState<UploadProgress>({ loaded: 0, total: null, percent: null })
  const [error, setError] = useState('')

  useEffect(() => {
    if (open) {
      setFile(null)
      setProgress({ loaded: 0, total: null, percent: null })
      setError('')
    }
    return () => abortRef.current?.abort()
  }, [open])

  const submit = async () => {
    if (!file || disabled) return
    setError('')
    const controller = new AbortController()
    abortRef.current = controller
    try {
      const result = await uploadBackup.mutateAsync({
        file,
        signal: controller.signal,
        onProgress: setProgress,
      })
      onUploaded(result)
      if (isUntrusted(result) || result.status === 'quarantined') {
        toast.success('Backup verified and placed in quarantine pending signer approval.')
      } else {
        toast.success('Backup uploaded and verified successfully.')
      }
      onOpenChange(false)
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') return
      setError(getBackupErrorMessage(err, 'The backup upload failed.'))
    } finally {
      abortRef.current = null
    }
  }

  const cancel = () => {
    if (uploadBackup.isPending) abortRef.current?.abort()
    else onOpenChange(false)
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !uploadBackup.isPending && onOpenChange(next)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Upload PPBase backup</DialogTitle>
          <DialogDescription>
            Select a signed PPBase ZIP. Unknown but valid signers remain quarantined until explicitly approved.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <Input
            type="file"
            accept=".zip,application/zip"
            disabled={disabled || uploadBackup.isPending}
            onChange={(event) => setFile(event.target.files?.[0] || null)}
          />
          {file && (
            <div className="rounded-lg border bg-slate-50 p-3 text-sm">
              <div className="font-medium">{file.name}</div>
              <div className="text-slate-500">{formatBytes(file.size)}</div>
            </div>
          )}
          {uploadBackup.isPending && (
            <div>
              <div className="mb-1.5 flex justify-between text-xs text-slate-600">
                <span>{progress.percent === null ? 'Uploading…' : `${Math.round(progress.percent)}%`}</span>
                <span>{formatBytes(progress.loaded)}{progress.total ? ` / ${formatBytes(progress.total)}` : ''}</span>
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-slate-200">
                <div
                  className={`h-full bg-indigo-600 transition-all ${progress.percent === null ? 'w-1/3 animate-pulse' : ''}`}
                  style={progress.percent === null ? undefined : { width: `${progress.percent}%` }}
                />
              </div>
              <p className="mt-2 text-xs text-slate-500">The server will verify the ZIP after transfer completes.</p>
            </div>
          )}
          {error && <p className="rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={cancel}>
            {uploadBackup.isPending ? 'Cancel upload' : 'Cancel'}
          </Button>
          <Button onClick={submit} disabled={disabled || !file || uploadBackup.isPending}>
            {uploadBackup.isPending && <LoadingSpinner size="sm" />}
            Upload and verify
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function BackupDetailsDialog({
  id,
  open,
  operationsDisabled,
  onOpenChange,
  onRestore,
}: {
  id: string | null
  open: boolean
  operationsDisabled: boolean
  onOpenChange: (open: boolean) => void
  onRestore: (backup: BackupInspection) => void
}) {
  const [resourceOffset, setResourceOffset] = useState(0)
  const { data, isLoading, isError, isFetching, refetch } = useBackup(
    open && id ? id : undefined,
    resourceOffset,
    BACKUP_RESOURCE_PAGE_SIZE,
  )
  const approveSigner = useApproveBackupSigner()
  const [error, setError] = useState('')

  useEffect(() => {
    setError('')
    setResourceOffset(0)
  }, [id, open])

  const returnedResources = data?.resources?.length || 0
  const effectiveResourceOffset = data?.resourceOffset ?? resourceOffset
  const totalResources = data?.resourceCount ?? returnedResources
  const firstResource = returnedResources ? effectiveResourceOffset + 1 : 0
  const lastResource = effectiveResourceOffset + returnedResources

  const approve = async () => {
    if (!id || !data?.signerFingerprintSha256 || operationsDisabled) return
    setError('')
    try {
      await approveSigner.mutateAsync({
        id,
        fingerprintSha256: data.signerFingerprintSha256,
      })
      await refetch()
      toast.success('Signer approved. Restore staging is now available.')
    } catch (err) {
      setError(getBackupErrorMessage(err, 'The signer could not be approved.'))
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[92vh] max-w-4xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Backup inspection</DialogTitle>
          <DialogDescription>Signed manifest, resources, integrity and provenance.</DialogDescription>
        </DialogHeader>
        {isLoading ? (
          <div className="flex min-h-48 items-center justify-center"><LoadingSpinner size="lg" /></div>
        ) : isError || !data ? (
          <div className="py-10 text-center">
            <XCircle className="mx-auto mb-3 h-9 w-9 text-red-500" />
            <p className="mb-3">The backup could not be inspected.</p>
            <Button variant="outline" onClick={() => refetch()}>Retry</Button>
          </div>
        ) : (
          <div className="space-y-5">
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <div className="rounded-lg border p-3"><div className="text-xs text-slate-500">Filename</div><div className="mt-1 break-all font-medium">{displayFilename(data)}</div></div>
              <div className="rounded-lg border p-3"><div className="text-xs text-slate-500">Created</div><div className="mt-1 font-medium">{formatDate(data.createdAt || data.modified)}</div></div>
              <div className="rounded-lg border p-3"><div className="text-xs text-slate-500">Transport size</div><div className="mt-1 font-medium">{formatBytes(data.size)}</div></div>
              <div className="rounded-lg border p-3"><div className="text-xs text-slate-500">Resources</div><div className="mt-1 font-medium">{data.resourceCount ?? data.resources?.length ?? '—'}</div></div>
            </div>

            <div className="rounded-xl border p-4">
              <div className="mb-2 flex items-center gap-2"><Fingerprint className="h-5 w-5 text-indigo-600" /><h3 className="font-semibold">Ed25519 provenance</h3></div>
              <div className="space-y-2 text-sm">
                <div><span className="text-slate-500">Trust: </span>{data.trustStatus || 'unknown'}</div>
                <code className="block break-all rounded bg-slate-100 p-2 text-xs">{data.signerFingerprintSha256 || 'No fingerprint'}</code>
                {data.signerPublicKey && <code className="block break-all rounded bg-slate-100 p-2 text-xs">{data.signerPublicKey}</code>}
              </div>
              {isUntrusted(data) && data.signerFingerprintSha256 && (
                <div className="mt-3">
                  <Button onClick={approve} disabled={operationsDisabled || approveSigner.isPending}>
                    {approveSigner.isPending && <LoadingSpinner size="sm" />}
                    Approve this exact Ed25519 key
                  </Button>
                </div>
              )}
              {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
            </div>

            <div>
              <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                <div>
                  <h3 className="font-semibold">Signed resources</h3>
                  <p className="text-xs text-slate-500">
                    Showing {firstResource}–{lastResource} of {totalResources}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={effectiveResourceOffset === 0 || isFetching}
                    onClick={() => setResourceOffset(Math.max(
                      0,
                      effectiveResourceOffset - BACKUP_RESOURCE_PAGE_SIZE,
                    ))}
                  >
                    Previous
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={!data.hasMoreResources || isFetching}
                    onClick={() => setResourceOffset(effectiveResourceOffset + returnedResources)}
                  >
                    Next
                  </Button>
                </div>
              </div>
              <div className="max-h-64 overflow-auto rounded-xl border">
                <Table>
                  <TableHeader><TableRow><TableHead>Path</TableHead><TableHead>Size</TableHead><TableHead>SHA-256</TableHead></TableRow></TableHeader>
                  <TableBody>
                    {(data.resources || []).map((resource) => (
                      <TableRow key={resource.path}>
                        <TableCell className="font-mono text-xs">{resource.path}</TableCell>
                        <TableCell>{formatBytes(resource.size)}</TableCell>
                        <TableCell className="max-w-64 truncate font-mono text-xs" title={resource.sha256}>{resource.sha256}</TableCell>
                      </TableRow>
                    ))}
                    {!data.resources?.length && <TableRow><TableCell colSpan={3} className="text-center text-slate-500">No resource details returned.</TableCell></TableRow>}
                  </TableBody>
                </Table>
              </div>
            </div>

            <div>
              <h3 className="mb-2 font-semibold">Manifest metadata</h3>
              <pre className="max-h-72 overflow-auto rounded-xl bg-slate-950 p-4 text-xs text-slate-100">{JSON.stringify(data.metadata || {}, null, 2)}</pre>
            </div>
          </div>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Close</Button>
          {data && (
            <Button
              onClick={() => onRestore(data)}
              disabled={operationsDisabled || isUntrusted(data) || data.integrityStatus === 'invalid'}
            >
              <RotateCcw className="h-4 w-4" />Restore
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

const ACTIVATION_PHASES = [
  'activating',
  'restarting',
  'health_check',
  'rolling_back',
  'rollback_restart',
  'rollback_health_check',
]

function activationPhaseLabel(value?: string) {
  const labels: Record<string, string> = {
    queued: 'Queued',
    activating: 'Switching targets',
    restarting: 'Restarting PPBase',
    health_check: 'Running health check',
    rolling_back: 'Restoring previous targets',
    rollback_restart: 'Restarting after rollback',
    rollback_health_check: 'Validating rollback',
    succeeded: 'Activation succeeded',
    rolled_back: 'Rollback succeeded',
    failed: 'Activation failed',
    action_required: 'Operator action required',
  }
  return labels[value || ''] || (value || 'Waiting for status').replace(/_/g, ' ')
}

function activationIsTerminal(status?: string) {
  return ['succeeded', 'rolled_back', 'failed', 'action_required'].includes(status || '')
}

function ActivationMonitor({
  resume,
  resumePersisted,
  onAccepted,
  onClear,
}: {
  resume: ActivationResumeState
  resumePersisted: boolean
  onAccepted: () => void
  onClear: () => void
}) {
  const { logout } = useAuth()
  const handledTerminal = useRef(false)
  const { data, error, isError, isFetching, refetch } = useBackupActivation(
    resume.activationId,
    resume.resumeToken,
  )
  const responseMatches = !!data && activationStatusMatchesResume(data, resume)
  const activationMissing = isError && errorStatus(error) === 404
  const status = responseMatches ? data.status : undefined
  const phase = responseMatches ? data.phase || status : undefined
  const displayedPhase = activationIsTerminal(status) ? status : phase

  useEffect(() => {
    if (responseMatches) onAccepted()
  }, [onAccepted, responseMatches])

  useEffect(() => {
    if (!activationShouldClearResume(status) || handledTerminal.current) return
    handledTerminal.current = true
    saveActivationResume(null)
    if (status !== 'succeeded') return
    toast.success('Backup activation completed and passed its health check.')
    if (resume.mode === 'clone') {
      logout()
      return
    }
    window.setTimeout(() => window.location.reload(), 1200)
  }, [logout, resume.mode, status])

  const currentIndex = ACTIVATION_PHASES.indexOf(phase || '')
  const rolledBack = status === 'rolled_back'
  const failed = status === 'failed' || status === 'action_required'

  return (
    <div className={`rounded-xl border p-4 ${
      status === 'succeeded' ? 'border-emerald-200 bg-emerald-50'
        : rolledBack ? 'border-amber-200 bg-amber-50'
          : failed ? 'border-red-200 bg-red-50' : 'border-indigo-200 bg-indigo-50'
    }`}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <div className="flex items-center gap-2 font-semibold">
            {status === 'succeeded' ? <CheckCircle2 className="h-5 w-5 text-emerald-600" />
              : failed ? <ShieldAlert className="h-5 w-5 text-red-600" />
                : <RefreshCw className={`h-5 w-5 text-indigo-600 ${activationIsTerminal(status) ? '' : 'animate-spin'}`} />}
            {data && !responseMatches
              ? 'Activation response mismatch'
              : activationPhaseLabel(displayedPhase)}
          </div>
          <p className="mt-1 text-sm text-slate-600">
            {data && !responseMatches
              ? 'The returned activation does not match the saved browser intent. Operations remain blocked.'
              : data?.message || `Restoring ${resume.filename} in ${resume.mode === 'clone' ? 'clone' : 'disaster recovery'} mode.`}
          </p>
          <p className="mt-1 font-mono text-xs text-slate-500">Activation {resume.activationId}</p>
          {!resumePersisted && (
            <p className="mt-2 rounded bg-amber-100 px-2 py-1.5 text-xs text-amber-900">
              This browser denied session storage. Keep this page open: reloading cannot resume activation tracking.
            </p>
          )}
        </div>
        <div className="flex gap-2">
          {isError && (
            <Button size="sm" variant="outline" onClick={() => refetch()} disabled={isFetching}>
              Retry status
            </Button>
          )}
          {((activationMissing && !isFetching)
            || (activationIsTerminal(status) && status !== 'succeeded')) && (
            <Button size="sm" variant="outline" onClick={onClear}>Dismiss</Button>
          )}
        </div>
      </div>
      {!activationIsTerminal(status) && (
        <div className="mt-4 grid grid-cols-3 gap-1 sm:grid-cols-6">
          {ACTIVATION_PHASES.map((item, index) => (
            <div key={item} className="text-center">
              <div className={`mx-auto mb-1 h-1.5 rounded-full ${index <= currentIndex ? 'bg-indigo-600' : 'bg-slate-200'}`} />
              <span className="text-[10px] text-slate-500">{activationPhaseLabel(item)}</span>
            </div>
          ))}
        </div>
      )}
      {responseMatches && data.actionRequired && <p className="mt-3 rounded bg-white p-2 text-sm text-red-700">{data.actionRequired}</p>}
      {responseMatches && (data.errorCode || data.failureCode) && (
        <p className="mt-3 rounded bg-white p-2 font-mono text-xs text-red-700">
          Failure code: {data.errorCode || data.failureCode}
        </p>
      )}
      {activationMissing && !data ? (
        <p className="mt-3 text-sm text-slate-600">
          No canonical activation exists for this intent. Dismiss it to return to the retained staging plan.
        </p>
      ) : isError && !data && (
        <p className="mt-3 text-sm text-red-700">
          {getBackupErrorMessage(
            error,
            'PPBase may be restarting. Status polling will continue with the scoped activation token.',
          )}
        </p>
      )}
    </div>
  )
}

function TrustStore({
  entries,
  disabled,
  onRevoke,
}: {
  entries: BackupTrustEntry[]
  disabled: boolean
  onRevoke: (entry: BackupTrustEntry) => void
}) {
  return (
    <div className="rounded-xl border bg-white p-4">
      <div className="mb-3 flex items-center gap-2"><ShieldCheck className="h-5 w-5 text-indigo-600" /><h2 className="font-semibold">Approved external signers</h2></div>
      {entries.length ? (
        <div className="space-y-2">
          {entries.map((entry) => (
            <div key={entry.fingerprintSha256} className="flex items-center gap-3 rounded-lg bg-slate-50 px-3 py-2">
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-medium">{entry.label || 'External PPBase server'}</div>
                <div className="truncate font-mono text-xs text-slate-500" title={entry.fingerprintSha256}>{entry.fingerprintSha256}</div>
              </div>
              <Button
                variant="ghost"
                size="icon"
                onClick={() => onRevoke(entry)}
                disabled={disabled}
                aria-label="Revoke signer"
              ><Trash2 className="h-4 w-4" /></Button>
            </div>
          ))}
        </div>
      ) : <p className="text-sm text-slate-500">No external signer has been approved.</p>}
    </div>
  )
}

function BackupAutomationCard({ disabled }: { disabled: boolean }) {
  const { data: settings = {} } = useSettings()
  const updateSettings = useUpdateSettings()
  const [cron, setCron] = useState('')
  const [maxKeep, setMaxKeep] = useState('3')
  const [error, setError] = useState('')

  useEffect(() => {
    const root = settings && typeof settings === 'object'
      ? settings as Record<string, unknown>
      : {}
    const raw = root.backups
    const backups = raw && typeof raw === 'object'
      ? raw as Record<string, unknown>
      : {}
    setCron(String(backups.cron ?? ''))
    setMaxKeep(String(backups.cronMaxKeep ?? 3))
  }, [settings])

  const parsedMaxKeep = Number(maxKeep)
  const retentionValid = Number.isInteger(parsedMaxKeep)
    && parsedMaxKeep >= 0
    && parsedMaxKeep <= 10000

  const save = async () => {
    if (!retentionValid || disabled) return
    setError('')
    try {
      await updateSettings.mutateAsync({
        backups: {
          cron: cron.trim(),
          cronMaxKeep: parsedMaxKeep,
        },
      })
      toast.success(cron.trim() ? 'Automatic backups scheduled.' : 'Automatic backups disabled.')
    } catch (err) {
      setError(getBackupErrorMessage(err, 'Automatic backup settings could not be saved.'))
    }
  }

  return (
    <div className="rounded-xl border bg-white p-4">
      <div className="mb-3 flex items-center gap-2">
        <Clock3 className="h-5 w-5 text-indigo-600" />
        <h2 className="font-semibold">Automatic local backups</h2>
      </div>
      <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_180px_auto] md:items-end">
        <div>
          <Label htmlFor="automatic-backup-cron">UTC cron schedule</Label>
          <Input
            id="automatic-backup-cron"
            className="mt-1.5 font-mono"
            value={cron}
            onChange={(event) => setCron(event.target.value)}
            placeholder="0 2 * * *"
            disabled={disabled || updateSettings.isPending}
          />
        </div>
        <div>
          <Label htmlFor="automatic-backup-max-keep">Maximum to keep</Label>
          <Input
            id="automatic-backup-max-keep"
            type="number"
            min={0}
            max={10000}
            className="mt-1.5"
            value={maxKeep}
            onChange={(event) => setMaxKeep(event.target.value)}
            disabled={disabled || updateSettings.isPending}
          />
        </div>
        <Button
          type="button"
          onClick={save}
          disabled={disabled || !retentionValid || updateSettings.isPending}
        >
          {updateSettings.isPending && <LoadingSpinner size="sm" />}
          Save schedule
        </Button>
      </div>
      <p className="mt-2 text-xs text-slate-500">
        Leave the cron empty to disable automation. A retention value of 0 keeps every automatic backup.
      </p>
      {!retentionValid && (
        <p className="mt-2 text-sm text-red-600">Retention must be an integer between 0 and 10000.</p>
      )}
      {error && <p className="mt-2 rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}
    </div>
  )
}

export function BackupsPage() {
  const { data: backups = [], isLoading, isError, error: backupsError, refetch } = useBackups()
  const { data: identity, error: identityError } = useBackupIdentity()
  const { data: readiness, error: readinessError } = useBackupReadiness()
  const { data: trustEntries = [] } = useBackupTrust()
  const deleteBackup = useDeleteBackup()
  const revokeSigner = useRevokeBackupSigner()
  const abandonStagingPlan = useAbandonBackupStagingPlan()

  const [createOpen, setCreateOpen] = useState(false)
  const [uploadOpen, setUploadOpen] = useState(false)
  const [detailsId, setDetailsId] = useState<string | null>(null)
  const [restoreBackup, setRestoreBackup] = useState<BackupListItem | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<BackupListItem | null>(null)
  const [revokeTarget, setRevokeTarget] = useState<BackupTrustEntry | null>(null)
  const [downloadingId, setDownloadingId] = useState<string | null>(null)
  const [activationResume, setActivationResume] = useState<ActivationResumeState | null>(readActivationResume)
  const [activationResumePersisted, setActivationResumePersisted] = useState(true)
  const [stagingResume, setStagingResume] = useState<StagingResumeState | null>(readStagingResume)
  const [stagingResumePersisted, setStagingResumePersisted] = useState(true)
  const abandonedPlanNoticeRef = useRef<string | null>(null)
  const stagingPlanQuery = useBackupStagingPlan(stagingResume?.planId)
  const activationQuery = useBackupActivation(
    activationResume?.activationId,
    activationResume?.resumeToken,
  )

  const activationResponseMatches = !!activationResume
    && !!activationQuery.data
    && activationStatusMatchesResume(activationQuery.data, activationResume)
  const activationBlocksOperations = activationBlocksBackupOperations(
    activationResume,
    activationResponseMatches ? activationQuery.data : undefined,
  )
  const readinessView = useMemo(
    () => readiness ? buildBackupReadinessView(readiness) : null,
    [readiness],
  )
  const canonicalStagingPlan = stagingResume
    && stagingPlanQuery.data
    && stagingPlanMatchesResume(stagingPlanQuery.data, stagingResume)
    ? stagingPlanQuery.data
    : null
  const stagingStatus = isCanonicalStagingStatus(canonicalStagingPlan?.status)
    ? canonicalStagingPlan.status
    : null

  const sortedBackups = useMemo(() => [...backups].sort((a, b) => {
    const left = new Date(a.modified || a.createdAt || 0).getTime()
    const right = new Date(b.modified || b.createdAt || 0).getTime()
    return right - left
  }), [backups])
  const stagedBackup = useMemo(
    () => stagingResume
      ? backups.find((backup) => backupKey(backup) === stagingResume.backupId) || {
        id: stagingResume.backupId,
        key: stagingResume.backupId,
        filename: stagingResume.filename,
      }
      : null,
    [backups, stagingResume],
  )

  const startDownload = async (backup: BackupListItem) => {
    if (activationBlocksOperations) return
    const id = backupKey(backup)
    setDownloadingId(id)
    try {
      await downloadBackup(id, backup.filename)
    } catch (err) {
      toast.error(getBackupErrorMessage(err, 'The backup download could not be started.'))
    } finally {
      setDownloadingId(null)
    }
  }

  const confirmDelete = async () => {
    if (!deleteTarget || activationBlocksOperations) return
    try {
      await deleteBackup.mutateAsync(backupKey(deleteTarget))
      toast.success(`${displayFilename(deleteTarget)} deleted.`)
      setDeleteTarget(null)
    } catch (err) {
      toast.error(getBackupErrorMessage(err, 'The backup could not be deleted.'))
    }
  }

  const confirmRevoke = async () => {
    if (!revokeTarget || activationBlocksOperations) return
    try {
      await revokeSigner.mutateAsync(revokeTarget.fingerprintSha256)
      toast.success('Backup signer trust revoked.')
      setRevokeTarget(null)
    } catch (err) {
      toast.error(getBackupErrorMessage(err, 'The signer could not be revoked.'))
    }
  }

  const stagingResumeChanged = useCallback((state: StagingResumeState | null) => {
    const persisted = saveStagingResume(state)
    setStagingResume(state)
    setStagingResumePersisted(state ? persisted : true)
    return persisted
  }, [])

  const activationResumeChanged = useCallback((state: ActivationResumeState | null) => {
    const persisted = saveActivationResume(state)
    setActivationResume(state)
    setActivationResumePersisted(state ? persisted : true)
    return persisted
  }, [])

  const activationAccepted = useCallback(() => {
    saveStagingResume(null)
    setStagingResume(null)
    setStagingResumePersisted(true)
  }, [])

  useEffect(() => {
    if (!stagingResume || stagingStatus !== 'abandoned') return
    if (abandonedPlanNoticeRef.current === stagingResume.planId) return
    abandonedPlanNoticeRef.current = stagingResume.planId
    stagingResumeChanged(null)
    toast.info('The saved restore staging plan was already abandoned on the server and was cleared locally.')
  }, [stagingResume, stagingResumeChanged, stagingStatus])

  const abandonSavedStaging = async () => {
    if (!stagingResume || stagingStatus === 'running' || abandonStagingPlan.isPending) return
    try {
      await abandonStagingPlan.mutateAsync({
        planId: stagingResume.planId,
        planHash: stagingResume.planHash,
      })
    } catch (err) {
      if (!err || typeof err !== 'object' || (err as { status?: unknown }).status !== 404) {
        toast.error(getBackupErrorMessage(err, 'The staging plan could not be abandoned safely.'))
        return
      }
    }
    stagingResumeChanged(null)
    toast.success('Restore staging plan abandoned.')
  }

  return (
    <>
      <ContentHeader
        left={<Breadcrumb items={[{ label: 'Settings' }, { label: 'Backups', active: true }]} />}
        right={
          <>
            <Button variant="outline" size="sm" onClick={() => refetch()} disabled={isLoading}>
              <RefreshCw className={`h-4 w-4 ${isLoading ? 'animate-spin' : ''}`} />Refresh
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setUploadOpen(true)}
              disabled={activationBlocksOperations}
            >
              <Upload className="h-4 w-4" />Upload ZIP
            </Button>
            <Button
              size="sm"
              onClick={() => setCreateOpen(true)}
              disabled={activationBlocksOperations || readinessView?.createBlocked === true}
              title={
                activationBlocksOperations
                  ? 'Backup operations are locked while activation status is unknown or in progress.'
                  : readinessView?.createBlocked
                  ? 'Configure the dedicated pg_dump role and local storage first.'
                  : undefined
              }
            >
              <Plus className="h-4 w-4" />Create backup
            </Button>
          </>
        }
      />

      <div className="flex-1 space-y-5 overflow-auto p-4 md:p-6">
        {activationResume && (
          <ActivationMonitor
            resume={activationResume}
            resumePersisted={activationResumePersisted}
            onAccepted={activationAccepted}
            onClear={() => {
              activationResumeChanged(null)
            }}
          />
        )}

        {stagingResume && !activationResume && (
          <div className={`rounded-xl border p-4 ${
            stagingStatus === 'validated' ? 'border-emerald-200 bg-emerald-50'
              : stagingStatus === 'failed' || stagingStatus === 'quarantined'
                ? 'border-red-200 bg-red-50'
                : 'border-indigo-200 bg-indigo-50'
          }`}>
            <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
              <div>
                <div className="flex items-center gap-2 font-semibold">
                  {stagingStatus === 'validated' ? <CheckCircle2 className="h-5 w-5 text-emerald-600" />
                    : stagingStatus === 'failed' || stagingStatus === 'quarantined'
                      ? <ShieldAlert className="h-5 w-5 text-red-600" />
                      : <RefreshCw className={`h-5 w-5 text-indigo-600 ${stagingStatus === 'running' ? 'animate-spin' : ''}`} />}
                  {stagingPlanQuery.isLoading ? 'Checking saved staging plan'
                    : stagingPlanQuery.isError ? 'Saved staging plan needs attention'
                      : stagingStatus === 'planned' ? 'Staging plan ready to execute'
                        : stagingStatus === 'running' ? 'Restore staging is running'
                          : stagingStatus === 'validated' ? 'Validated staging plan ready'
                            : stagingStatus === 'failed' ? 'Restore staging failed'
                              : stagingStatus === 'quarantined' ? 'Restore staging quarantined'
                                : 'Saved staging status is unknown'}
                </div>
                <p className="mt-1 text-sm text-slate-700">
                  {stagingResume.filename} · {stagingResume.mode === 'clone' ? 'Clone' : 'Disaster recovery'}
                </p>
                <p className="mt-1 font-mono text-xs text-slate-600">Plan {stagingResume.planId}</p>
                {stagingStatus === 'running' && (
                  <p className="mt-2 text-xs text-indigo-900">
                    Canonical status is polled automatically. Abandon is disabled until execution finishes.
                  </p>
                )}
                {(stagingStatus === 'failed' || stagingStatus === 'quarantined') && (
                  <p className="mt-2 text-xs text-red-800">
                    The isolated targets were not activated. Review the failure, then abandon this plan client and server side.
                  </p>
                )}
                {stagingPlanQuery.isError && (
                  <p className="mt-2 text-xs text-amber-900">
                    The canonical plan could not be loaded. Resume to retry inspection or abandon it safely.
                  </p>
                )}
                {!stagingResumePersisted && (
                  <p className="mt-2 text-xs text-amber-900">
                    Session storage is unavailable. This plan can be resumed while this page remains open, but not after a reload.
                  </p>
                )}
              </div>
              <div className="flex flex-wrap gap-2">
                <Button
                  type="button"
                  size="sm"
                  onClick={() => stagedBackup && setRestoreBackup(stagedBackup)}
                  disabled={!stagedBackup || activationBlocksOperations}
                >
                  {stagingStatus === 'validated' ? 'Resume activation' : 'Resume restore'}
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={abandonSavedStaging}
                  disabled={
                    stagingPlanQuery.isLoading
                    || stagingStatus === 'running'
                    || abandonStagingPlan.isPending
                  }
                >
                  {abandonStagingPlan.isPending && <LoadingSpinner size="sm" />}
                  Abandon
                </Button>
              </div>
            </div>
          </div>
        )}

        {readiness && readinessView?.ready && (
          <div className="space-y-2" aria-label="Native backup readiness">
            <div className="flex items-center gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm font-semibold text-emerald-900">
              <ShieldCheck className="h-5 w-5 shrink-0 text-emerald-600" />
              {readinessView.successText}
            </div>
            {readinessView.warnings.length > 0 && (
              <div className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-950" role="status">
                <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
                <div>
                  {readinessView.warnings.map((warning) => (
                    <p key={`${warning.code}:${warning.name}`}>
                      <span className="font-medium">{warning.name}:</span> {warning.detail}
                    </p>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
        {readiness && readinessView && !readinessView.ready && (
          <div className="rounded-xl border border-red-200 bg-white p-4">
            <div className="flex items-center gap-2 text-red-800">
              <ShieldAlert className="h-5 w-5 shrink-0" />
              <h2 className="font-semibold">Backup & restore needs setup</h2>
            </div>
            <ul className="mt-3 divide-y divide-red-100 border-y border-red-100">
              {readinessView.blockers.map((blocker) => (
                <li key={blocker.action} className="py-2.5 text-sm">
                  <div className="font-medium text-slate-900">{blocker.action}</div>
                  <div className="mt-0.5 text-xs text-slate-500">
                    Required for {blocker.areas.join(', ')}
                  </div>
                </li>
              ))}
            </ul>
            <details className="mt-3 text-sm text-slate-600">
              <summary className="cursor-pointer select-none font-medium text-slate-700">
                Show details
              </summary>
              <div className="mt-3 space-y-3 border-t pt-3">
                <div className="space-y-1 text-xs">
                  {readinessView.details.map(({ key, label, check }) => (
                    <div key={key} className="flex items-start justify-between gap-3">
                      <span>{label}</span>
                      <span className={check.configured ? 'text-emerald-700' : 'text-red-700'}>
                        {check.configured ? 'Ready' : 'Needs setup'}
                      </span>
                    </div>
                  ))}
                </div>
                {readinessView.warnings.map((warning) => (
                  <p key={`${warning.code}:${warning.name}`} className="text-xs text-amber-800">
                    {warning.name}: {warning.detail}
                  </p>
                ))}
                <div>
                  <div className="text-xs font-medium text-slate-700">Recommended setup</div>
                  <code className="mt-1 block break-all rounded bg-slate-50 px-2 py-1.5 text-[11px] text-slate-700">
                    {readiness.onboarding.productionCommand}
                  </code>
                  <code className="mt-1 block break-all rounded bg-slate-50 px-2 py-1.5 text-[11px] text-slate-700">
                    {readiness.onboarding.doctorCommand}
                  </code>
                </div>
              </div>
            </details>
          </div>
        )}
        {!readiness && readinessError && (
          <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700">
            {getBackupErrorMessage(readinessError, 'Backup readiness is unavailable.')}
          </div>
        )}

        <BackupAutomationCard disabled={activationBlocksOperations} />

        <div className="grid gap-4 lg:grid-cols-2">
          <div className="rounded-xl border bg-white p-4">
            <div className="mb-3 flex items-center gap-2"><Fingerprint className="h-5 w-5 text-indigo-600" /><h2 className="font-semibold">This server identity</h2></div>
            {identity ? (
              <>
                <div className="mb-1 text-sm text-slate-500">Ed25519 / SHA-256 fingerprint</div>
                <code className="block break-all rounded-lg bg-slate-50 p-3 text-xs">{identity.fingerprintSha256}</code>
              </>
            ) : <p className="text-sm text-red-600">{getBackupErrorMessage(identityError, 'Identity unavailable.')}</p>}
          </div>
          <TrustStore
            entries={trustEntries}
            disabled={activationBlocksOperations}
            onRevoke={setRevokeTarget}
          />
        </div>

        <div className="rounded-xl border bg-white shadow-sm">
          <div className="flex items-center gap-2 border-b px-4 py-3">
            <ArchiveRestore className="h-5 w-5 text-indigo-600" />
            <div>
              <h1 className="font-semibold">Backup and restore PPBase data</h1>
              <p className="text-sm text-slate-500">Signed PostgreSQL dumps, local files and restore validation.</p>
            </div>
          </div>

          {isLoading ? (
            <div className="flex min-h-64 items-center justify-center"><LoadingSpinner size="lg" /></div>
          ) : isError ? (
            <div className="py-16 text-center">
              <XCircle className="mx-auto mb-3 h-10 w-10 text-red-500" />
              <h3 className="font-semibold">Failed to load backups</h3>
              <p className="mb-4 text-sm text-red-600">
                {getBackupErrorMessage(backupsError, 'The native backup API is unavailable.')}
              </p>
              <Button variant="outline" onClick={() => refetch()}>Retry</Button>
            </div>
          ) : sortedBackups.length === 0 ? (
            <div className="p-5">
              <EmptyState
                icon={<FileArchive className="h-6 w-6" />}
                title="No backups yet"
                description="Create a signed local backup or upload a PPBase ZIP from another server."
              />
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Backup</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Provenance</TableHead>
                  <TableHead>Size</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {sortedBackups.map((backup) => {
                  const id = backupKey(backup)
                  const unusable = backup.status === 'invalid' || backup.integrityStatus === 'invalid'
                  return (
                    <TableRow key={id}>
                      <TableCell>
                        <div className="max-w-72 truncate font-medium" title={displayFilename(backup)}>{displayFilename(backup)}</div>
                        <div className="max-w-72 truncate font-mono text-xs text-slate-500" title={backup.signerFingerprintSha256 || ''}>{shortFingerprint(backup.signerFingerprintSha256)}</div>
                      </TableCell>
                      <TableCell><StatusBadge backup={backup} /></TableCell>
                      <TableCell><TrustBadge backup={backup} /></TableCell>
                      <TableCell>{formatBytes(backup.size ?? backup.totalSize)}</TableCell>
                      <TableCell>{formatDate(backup.modified || backup.createdAt)}</TableCell>
                      <TableCell>
                        <div className="flex justify-end gap-1">
                          <Button variant="ghost" size="icon" onClick={() => setDetailsId(id)} aria-label="Inspect backup"><Eye className="h-4 w-4" /></Button>
                          <Button
                            variant="ghost"
                            size="icon"
                            onClick={() => startDownload(backup)}
                            disabled={activationBlocksOperations || downloadingId === id || unusable}
                            aria-label="Download backup"
                          >
                            {downloadingId === id ? <LoadingSpinner size="sm" /> : <Download className="h-4 w-4" />}
                          </Button>
                          <Button
                            variant="ghost"
                            size="icon"
                            onClick={() => setRestoreBackup(backup)}
                            disabled={
                              isUntrusted(backup)
                              || unusable
                              || activationBlocksOperations
                              || (!!stagingResume && stagingResume.backupId !== id)
                              || readinessView?.restoreBlocked === true
                            }
                            aria-label="Restore backup"
                          ><RotateCcw className="h-4 w-4" /></Button>
                          <Button
                            variant="ghost"
                            size="icon"
                            className="text-red-600 hover:text-red-700"
                            onClick={() => setDeleteTarget(backup)}
                            disabled={activationBlocksOperations}
                            aria-label="Delete backup"
                          ><Trash2 className="h-4 w-4" /></Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          )}
        </div>
      </div>

      <CreateBackupDialog
        open={createOpen}
        disabled={activationBlocksOperations}
        onOpenChange={setCreateOpen}
      />
      <UploadBackupDialog
        open={uploadOpen}
        disabled={activationBlocksOperations}
        onOpenChange={setUploadOpen}
        onUploaded={(inspection) => setDetailsId(inspection.id || inspection.key)}
      />
      <BackupDetailsDialog
        id={detailsId}
        open={!!detailsId}
        operationsDisabled={activationBlocksOperations}
        onOpenChange={(open) => !open && setDetailsId(null)}
        onRestore={(backup) => {
          setDetailsId(null)
          setRestoreBackup(backup)
        }}
      />
      <BackupRestoreWizard
        backup={restoreBackup}
        open={!!restoreBackup}
        onOpenChange={(open) => !open && setRestoreBackup(null)}
        stagingResume={stagingResume}
        onStagingResumeChange={stagingResumeChanged}
        onActivationResumeChange={activationResumeChanged}
        onActivationAccepted={activationAccepted}
      />
      <ConfirmDialog
        open={!!deleteTarget}
        onOpenChange={(open) => !open && setDeleteTarget(null)}
        title="Delete backup"
        description={`Delete ${deleteTarget ? displayFilename(deleteTarget) : 'this backup'} permanently? A backup currently used by staging or download will be refused by the server.`}
        confirmLabel="Delete"
        variant="destructive"
        onConfirm={confirmDelete}
      />
      <ConfirmDialog
        open={!!revokeTarget}
        onOpenChange={(open) => !open && setRevokeTarget(null)}
        title="Revoke backup signer"
        description={`Revoke trust for ${revokeTarget?.label || revokeTarget?.fingerprintSha256 || 'this signer'}? External backups signed by this exact key will no longer be restorable.`}
        confirmLabel="Revoke"
        variant="destructive"
        onConfirm={confirmRevoke}
      />
    </>
  )
}
