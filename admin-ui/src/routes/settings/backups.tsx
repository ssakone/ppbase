import { useEffect, useMemo, useRef, useState } from 'react'
import {
  ArchiveRestore,
  Clock3,
  Download,
  FileArchive,
  Plus,
  RefreshCw,
  RotateCcw,
  ShieldAlert,
  Trash2,
  Upload,
  XCircle,
} from 'lucide-react'
import { toast } from 'sonner'
import type { UploadProgress } from '@/api/client'
import type { BackupListItem } from '@/api/types'
import { backupKey, downloadBackup, getBackupErrorMessage } from '@/api/endpoints/backups'
import {
  useBackupReadiness,
  useBackups,
  useBackupOperationAvailability,
  useCreateBackup,
  useDeleteBackup,
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
import { buildBackupReadinessView } from '@/lib/backup-readiness-view'
import {
  BACKUP_DIALOG_AUTO_CLOSE_MS,
  isBackupOperationAvailable,
} from '@/lib/backup-operation-view'
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
  const closeTimerRef = useRef<number | null>(null)

  const clearCloseTimer = () => {
    if (closeTimerRef.current === null) return
    window.clearTimeout(closeTimerRef.current)
    closeTimerRef.current = null
  }

  useEffect(() => {
    if (open) {
      setName('')
      setError('')
    }
  }, [open])

  useEffect(() => () => clearCloseTimer(), [])

  const validName = !name || (name.endsWith('.zip') && !/[\\/\0]/.test(name))

  const submit = async () => {
    if (!validName || disabled) return
    setError('')
    let settled = false
    let continuedInBackground = false
    clearCloseTimer()
    closeTimerRef.current = window.setTimeout(() => {
      if (settled) return
      continuedInBackground = true
      toast.info('The backup was started but may take a while to complete. You can come back later.')
      onOpenChange(false)
    }, BACKUP_DIALOG_AUTO_CLOSE_MS)
    try {
      await createBackup.mutateAsync(name.trim() || undefined)
      settled = true
      clearCloseTimer()
      toast.success('Backup created successfully.')
      onOpenChange(false)
    } catch (err) {
      settled = true
      clearCloseTimer()
      const message = getBackupErrorMessage(err, 'The backup could not be created.')
      if (continuedInBackground) toast.error(message)
      else setError(message)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !createBackup.isPending && onOpenChange(next)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Create backup</DialogTitle>
          <DialogDescription>
            PPBase temporarily blocks writes while PostgreSQL and local files are captured into a ZIP.
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
            <p className="mt-1.5 text-xs text-slate-500">
              Optional. Must be a safe basename ending in .zip.
            </p>
          </div>
          {!validName && (
            <p className="text-sm text-red-600">
              Use a basename ending in .zip without path separators.
            </p>
          )}
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
}: {
  open: boolean
  disabled: boolean
  onOpenChange: (open: boolean) => void
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
      await uploadBackup.mutateAsync({ file, signal: controller.signal, onProgress: setProgress })
      toast.success('Backup ZIP uploaded successfully.')
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
            Select a ZIP backup. Its internal metadata and checksums are validated before restore.
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
            Upload
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
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
        backups: { cron: cron.trim(), cronMaxKeep: parsedMaxKeep },
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
        <Button type="button" onClick={save} disabled={disabled || !retentionValid || updateSettings.isPending}>
          {updateSettings.isPending && <LoadingSpinner size="sm" />}
          Save schedule
        </Button>
      </div>
      <p className="mt-2 text-xs text-slate-500">
        Leave the cron empty to disable automation. A retention value of 0 keeps every backup.
      </p>
      {!retentionValid && (
        <p className="mt-2 text-sm text-red-600">Retention must be an integer between 0 and 10000.</p>
      )}
      {error && <p className="mt-2 rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}
    </div>
  )
}

export function BackupsPage() {
  const { logout } = useAuth()
  const { data: backups = [], isLoading, isError, error: backupsError, refetch } = useBackups()
  const { data: readiness, error: readinessError } = useBackupReadiness()
  const { data: operationHealth } = useBackupOperationAvailability()
  const deleteBackup = useDeleteBackup()

  const [createOpen, setCreateOpen] = useState(false)
  const [uploadOpen, setUploadOpen] = useState(false)
  const [restoreBackup, setRestoreBackup] = useState<BackupListItem | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<BackupListItem | null>(null)
  const [downloadingId, setDownloadingId] = useState<string | null>(null)
  const [restoringFilename, setRestoringFilename] = useState<string | null>(null)

  const readinessView = useMemo(
    () => readiness ? buildBackupReadinessView(readiness) : null,
    [readiness],
  )
  const restoring = restoringFilename !== null
  const canStartBackupOperation = isBackupOperationAvailable(operationHealth)
  const backupOperationActive = !canStartBackupOperation
  const createBlocked = readinessView?.createReady === false
  const restoreBlocked = readinessView?.restoreReady === false
  const previousCanStartRef = useRef<boolean | null>(null)

  useEffect(() => {
    if (previousCanStartRef.current === false && canStartBackupOperation) {
      void refetch()
    }
    previousCanStartRef.current = canStartBackupOperation
  }, [canStartBackupOperation, refetch])

  const sortedBackups = useMemo(() => [...backups].sort((a, b) => (
    new Date(b.modified).getTime() - new Date(a.modified).getTime()
  )), [backups])

  const handleRestoreStarted = (filename: string) => {
    setRestoringFilename(filename)
    toast.success('Restore committed. PPBase is restarting.')
    window.setTimeout(() => {
      logout()
      window.location.reload()
    }, 6000)
  }

  const startDownload = async (backup: BackupListItem) => {
    if (restoring) return
    const key = backupKey(backup)
    setDownloadingId(key)
    try {
      await downloadBackup(key, backup.key)
    } catch (err) {
      toast.error(getBackupErrorMessage(err, 'The backup download could not be started.'))
    } finally {
      setDownloadingId(null)
    }
  }

  const confirmDelete = async () => {
    if (!deleteTarget || restoring || backupOperationActive) return
    try {
      await deleteBackup.mutateAsync(backupKey(deleteTarget))
      toast.success(`${deleteTarget.key} deleted.`)
      setDeleteTarget(null)
    } catch (err) {
      toast.error(getBackupErrorMessage(err, 'The backup could not be deleted.'))
    }
  }

  return (
    <>
      <ContentHeader
        left={<Breadcrumb items={[{ label: 'Settings' }, { label: 'Backups', active: true }]} />}
        right={
          <>
            <Button variant="outline" size="sm" onClick={() => refetch()} disabled={isLoading || restoring}>
              <RefreshCw className={`h-4 w-4 ${isLoading ? 'animate-spin' : ''}`} />Refresh
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setUploadOpen(true)}
              disabled={restoring || backupOperationActive}
            >
              <Upload className="h-4 w-4" />Upload ZIP
            </Button>
            <Button
              size="sm"
              onClick={() => setCreateOpen(true)}
              disabled={restoring || backupOperationActive || createBlocked}
            >
              {backupOperationActive ? <LoadingSpinner size="sm" /> : <Plus className="h-4 w-4" />}
              {backupOperationActive ? 'Operation in progress…' : 'Create backup'}
            </Button>
          </>
        }
      />

      <div className="flex-1 space-y-5 overflow-auto p-4 md:p-6">
        {restoring && (
          <div className="flex items-center gap-3 rounded-xl border border-indigo-200 bg-indigo-50 px-4 py-3 text-sm text-indigo-900">
            <RefreshCw className="h-5 w-5 shrink-0 animate-spin text-indigo-600" />
            <div>
              <p className="font-semibold">Restoring {restoringFilename} and restarting PPBase…</p>
              <p className="mt-0.5 text-indigo-800">This page will reload when PPBase is back.</p>
            </div>
          </div>
        )}

        {backupOperationActive && !restoring && (
          <div className="flex items-center gap-3 rounded-xl border border-indigo-200 bg-indigo-50 px-4 py-3 text-sm text-indigo-900" role="status">
            <LoadingSpinner size="sm" />
            <div>
              <p className="font-semibold">Backup/restore operation is in process</p>
              <p className="mt-0.5 text-indigo-800">
                You can leave this page. The backup list will refresh automatically when the operation finishes.
              </p>
            </div>
          </div>
        )}

        {readinessView && (createBlocked || restoreBlocked) && (
          <div className="flex items-start gap-2 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">
            <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0 text-red-600" />
            <div>
              <p className="font-semibold">Backup prerequisites are unavailable</p>
              {createBlocked && <p className="mt-0.5">Create: {readinessView.createMissing.join(', ') || 'runtime prerequisite missing'}.</p>}
              {restoreBlocked && <p className="mt-0.5">Restore: {readinessView.restoreMissing.join(', ') || 'runtime prerequisite missing'}.</p>}
            </div>
          </div>
        )}

        {readinessView && readinessView.notes.length > 0 && (
          <div className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-950" role="status">
            <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
            <div className="space-y-1">
              {readinessView.notes.map((note) => <p key={note}>{note}</p>)}
            </div>
          </div>
        )}

        {!readiness && readinessError && (
          <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700">
            {getBackupErrorMessage(readinessError, 'Backup readiness is unavailable.')}
          </div>
        )}

        <BackupAutomationCard disabled={restoring || backupOperationActive} />

        <div className="rounded-xl border bg-white shadow-sm">
          <div className="flex items-center gap-2 border-b px-4 py-3">
            <ArchiveRestore className="h-5 w-5 text-indigo-600" />
            <div>
              <h1 className="font-semibold">Backup and restore PPBase data</h1>
              <p className="text-sm text-slate-500">
                Native PostgreSQL and local-file ZIP backups stored in pb_data/backups.
              </p>
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
                description="Create a local backup or upload a PPBase ZIP."
              />
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Backup</TableHead>
                  <TableHead>Size</TableHead>
                  <TableHead>Modified</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {sortedBackups.map((backup) => {
                  const key = backupKey(backup)
                  return (
                    <TableRow key={key}>
                      <TableCell>
                        <div className="max-w-96 truncate font-medium" title={backup.key}>{backup.key}</div>
                      </TableCell>
                      <TableCell>{formatBytes(backup.size)}</TableCell>
                      <TableCell>{formatDate(backup.modified)}</TableCell>
                      <TableCell>
                        <div className="flex justify-end gap-1">
                          <Button
                            variant="ghost"
                            size="icon"
                            onClick={() => startDownload(backup)}
                            disabled={restoring || downloadingId === key}
                            aria-label="Download backup"
                          >
                            {downloadingId === key ? <LoadingSpinner size="sm" /> : <Download className="h-4 w-4" />}
                          </Button>
                          <Button
                            variant="ghost"
                            size="icon"
                            onClick={() => setRestoreBackup(backup)}
                            disabled={restoring || backupOperationActive || restoreBlocked}
                            aria-label="Restore backup"
                          ><RotateCcw className="h-4 w-4" /></Button>
                          <Button
                            variant="ghost"
                            size="icon"
                            className="text-red-600 hover:text-red-700"
                            onClick={() => setDeleteTarget(backup)}
                            disabled={restoring || backupOperationActive}
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
        disabled={restoring || backupOperationActive || createBlocked}
        onOpenChange={setCreateOpen}
      />
      <UploadBackupDialog
        open={uploadOpen}
        disabled={restoring || backupOperationActive}
        onOpenChange={setUploadOpen}
      />
      <BackupRestoreWizard
        backup={restoreBackup}
        open={!!restoreBackup}
        disabled={backupOperationActive || restoreBlocked}
        onOpenChange={(open) => !open && setRestoreBackup(null)}
        onRestoreStarted={handleRestoreStarted}
      />
      <ConfirmDialog
        open={!!deleteTarget}
        onOpenChange={(open) => !open && setDeleteTarget(null)}
        title="Delete backup"
        description={`Delete ${deleteTarget?.key || 'this backup'} permanently?`}
        confirmLabel="Delete"
        variant="destructive"
        onConfirm={confirmDelete}
      />
    </>
  )
}
