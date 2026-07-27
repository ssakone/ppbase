import { useEffect, useState } from 'react'
import { AlertTriangle, Copy, RefreshCw, ShieldCheck } from 'lucide-react'
import { toast } from 'sonner'
import type { BackupListItem } from '@/api/types'
import { backupKey, getBackupErrorMessage } from '@/api/endpoints/backups'
import {
  useApproveBackupSigner,
  useBackup,
  useRestoreBackupDestructive,
} from '@/hooks/use-backups'
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
import { LoadingSpinner } from '@/components/loading-spinner'
import { isBackupTrusted } from '@/lib/backup-trust'

interface BackupRestoreWizardProps {
  backup: BackupListItem | null
  open: boolean
  disabled?: boolean
  onOpenChange: (open: boolean) => void
  onRestoreStarted: (filename: string) => void
}

export function BackupRestoreWizard({
  backup,
  open,
  disabled = false,
  onOpenChange,
  onRestoreStarted,
}: BackupRestoreWizardProps) {
  const id = backup ? backupKey(backup) : undefined
  const { data: inspection, isLoading, isError, refetch } = useBackup(open ? id : undefined)
  const approveSigner = useApproveBackupSigner()
  const restore = useRestoreBackupDestructive()

  const [confirmation, setConfirmation] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    setConfirmation('')
    setError('')
  }, [id, open])

  const filename = inspection?.filename || backup?.filename || backup?.key || id || ''
  const trusted = isBackupTrusted(inspection?.trustStatus, inspection?.authenticated)
  const invalid = inspection?.integrityStatus === 'invalid' || inspection?.status === 'invalid'
  const busy = approveSigner.isPending || restore.isPending

  const handleApprove = async () => {
    if (!id || !inspection?.signerFingerprintSha256) return
    setError('')
    try {
      await approveSigner.mutateAsync({
        id,
        fingerprintSha256: inspection.signerFingerprintSha256,
      })
      await refetch()
      toast.success('Backup signer approved.')
    } catch (err) {
      setError(getBackupErrorMessage(err, 'The backup signer could not be approved.'))
    }
  }

  const handleRestore = async () => {
    if (disabled || !id || !trusted || invalid || confirmation !== filename || restore.isPending) return
    setError('')
    try {
      await restore.mutateAsync(id)
    } catch (err) {
      setError(getBackupErrorMessage(
        err,
        'The restore request failed. Check PPBase status before retrying because the restore may already have committed.',
      ))
      return
    }
    onRestoreStarted(filename)
    onOpenChange(false)
  }

  const copyFingerprint = async () => {
    const value = inspection?.signerFingerprintSha256
    if (!value) return
    await navigator.clipboard.writeText(value)
    toast.success('Fingerprint copied.')
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !busy && onOpenChange(next)}>
      <DialogContent className="max-h-[92vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Restore {filename || 'backup'}</DialogTitle>
          <DialogDescription>
            This replaces the active database and file storage with the contents of this backup,
            then restarts PPBase.
          </DialogDescription>
        </DialogHeader>

        {isLoading ? (
          <div className="flex min-h-48 items-center justify-center">
            <LoadingSpinner size="lg" />
          </div>
        ) : isError || !inspection ? (
          <div className="flex min-h-48 flex-col items-center justify-center gap-3 text-center">
            <AlertTriangle className="h-9 w-9 text-red-500" />
            <div>
              <p className="font-semibold">The backup could not be inspected.</p>
              <p className="mt-1 text-sm text-slate-500">
                Restore remains blocked until integrity and provenance can be verified.
              </p>
            </div>
            <Button type="button" variant="outline" onClick={() => refetch()}>
              Retry inspection
            </Button>
          </div>
        ) : (
          <div className="space-y-5">
            <div className="rounded-xl border bg-slate-50 p-4">
              <div className="mb-3 flex flex-wrap items-center gap-2">
                <ShieldCheck className="h-5 w-5 text-indigo-600" />
                <span className="font-semibold">Integrity and provenance</span>
                <Badge variant={trusted ? 'secondary' : 'destructive'}>
                  {trusted ? 'Trusted signer' : 'Approval required'}
                </Badge>
                <Badge variant={invalid ? 'destructive' : 'outline'}>
                  {inspection.integrityStatus || (inspection.resourcesVerified ? 'valid' : 'unchecked')}
                </Badge>
              </div>
              <div className="flex items-start gap-2 text-xs text-slate-600">
                <code className="min-w-0 flex-1 break-all rounded bg-white px-2 py-1.5">
                  {inspection.signerFingerprintSha256 || 'No signer fingerprint'}
                </code>
                {inspection.signerFingerprintSha256 && (
                  <Button variant="ghost" size="icon" onClick={copyFingerprint} aria-label="Copy fingerprint">
                    <Copy className="h-4 w-4" />
                  </Button>
                )}
              </div>

              {!trusted && !invalid && inspection.signerFingerprintSha256 && (
                <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50 p-3">
                  <p className="mb-3 text-sm text-amber-900">
                    The signature is valid, but this external Ed25519 identity is not approved on this
                    server. Restore remains blocked until you approve it.
                  </p>
                  <Button
                    type="button"
                    onClick={handleApprove}
                    disabled={approveSigner.isPending}
                  >
                    {approveSigner.isPending && <LoadingSpinner size="sm" />}
                    Approve this exact Ed25519 key
                  </Button>
                </div>
              )}
            </div>

            <div className="rounded-xl border border-red-200 bg-red-50 p-4">
              <div className="mb-2 flex items-center gap-2 text-red-800">
                <AlertTriangle className="h-5 w-5" />
                <h3 className="font-semibold">Confirm destructive restore</h3>
              </div>
              <p className="mb-3 text-sm text-red-800">
                The archive is fully verified before anything changes. PPBase then blocks writes,
                replaces the active database and file storage with this backup, and restarts. On
                startup, PPBase finishes recovery and applies any newer migrations. Data currently
                in this instance will be lost. If the restore fails before its database commit, the
                previous files are put back and the database is not left partially restored.
              </p>
              <Label htmlFor="backup-restore-confirmation">
                Type <code className="rounded bg-white px-1.5 py-0.5">{filename}</code> to confirm
              </Label>
              <Input
                id="backup-restore-confirmation"
                className="mt-2"
                value={confirmation}
                onChange={(event) => setConfirmation(event.target.value)}
                autoComplete="off"
                disabled={disabled || !trusted || invalid || restore.isPending}
              />
              <Button
                type="button"
                variant="destructive"
                className="mt-3"
                onClick={handleRestore}
                disabled={
                  disabled
                  || !trusted
                  || invalid
                  || confirmation !== filename
                  || restore.isPending
                }
              >
                {restore.isPending ? <LoadingSpinner size="sm" /> : <RefreshCw className="h-4 w-4" />}
                Restore and restart PPBase
              </Button>
            </div>

            {error && (
              <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">
                {error}
              </div>
            )}
          </div>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={busy}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
