import { useEffect, useState } from 'react'
import { AlertTriangle, RefreshCw } from 'lucide-react'
import type { BackupListItem } from '@/api/types'
import { backupKey, getBackupErrorMessage } from '@/api/endpoints/backups'
import { useRestoreBackupDestructive } from '@/hooks/use-backups'
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
  const restore = useRestoreBackupDestructive()
  const [confirmation, setConfirmation] = useState('')
  const [error, setError] = useState('')
  const filename = backup?.key || ''

  useEffect(() => {
    setConfirmation('')
    setError('')
  }, [filename, open])

  const handleRestore = async () => {
    if (!backup || disabled || confirmation !== filename || restore.isPending) return
    setError('')
    try {
      await restore.mutateAsync(backupKey(backup))
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

  return (
    <Dialog open={open} onOpenChange={(next) => !restore.isPending && onOpenChange(next)}>
      <DialogContent className="max-h-[92vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Restore {filename || 'backup'}</DialogTitle>
          <DialogDescription>
            This replaces the active database and local file storage with the contents of this ZIP,
            then restarts PPBase.
          </DialogDescription>
        </DialogHeader>

        <div className="rounded-xl border border-red-200 bg-red-50 p-4">
          <div className="mb-2 flex items-center gap-2 text-red-800">
            <AlertTriangle className="h-5 w-5" />
            <h3 className="font-semibold">Confirm destructive restore</h3>
          </div>
          <p className="mb-3 text-sm text-red-800">
            PPBase validates backup.json and every resource checksum before changing anything. It
            then blocks writes, restores PostgreSQL and local files, and restarts. Current data in
            this instance will be replaced.
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
            disabled={disabled || restore.isPending}
          />
          <Button
            type="button"
            variant="destructive"
            className="mt-3"
            onClick={handleRestore}
            disabled={disabled || confirmation !== filename || restore.isPending}
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

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={restore.isPending}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
