import { useState } from 'react'
import type { MigrationStatus } from '@/api/types'
import { useApplyMigrations, useRevertMigration, useGenerateSnapshot } from '@/hooks/use-migrations'
import { Button } from '@/components/ui/button'
import { ConfirmDialog } from '@/components/confirm-dialog'
import { Check, ChevronLeft, Plus } from 'lucide-react'
import { toast } from 'sonner'

interface MigrationActionsProps {
  status: MigrationStatus | undefined
}

export function MigrationActions({ status }: MigrationActionsProps) {
  const [applyOpen, setApplyOpen] = useState(false)
  const [revertOpen, setRevertOpen] = useState(false)
  const [snapshotOpen, setSnapshotOpen] = useState(false)

  const applyMigrations = useApplyMigrations()
  const revertMigration = useRevertMigration()
  const generateSnapshot = useGenerateSnapshot()

  const handleApply = async () => {
    try {
      await applyMigrations.mutateAsync()
      toast.success('All pending migrations applied successfully.')
      setApplyOpen(false)
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to apply migrations.'
      toast.error(message)
    }
  }

  const handleRevert = async () => {
    try {
      await revertMigration.mutateAsync(1)
      toast.success('Last migration reverted successfully.')
      setRevertOpen(false)
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to revert migration.'
      toast.error(message)
    }
  }

  const handleSnapshot = async () => {
    try {
      await generateSnapshot.mutateAsync()
      toast.success('Snapshot migration generated successfully.')
      setSnapshotOpen(false)
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to generate snapshot.'
      toast.error(message)
    }
  }

  return (
    <>
      <div className="flex items-center gap-2">
        <Button onClick={() => setApplyOpen(true)} disabled={!status?.pending}>
          <Check className="mr-1.5 h-4 w-4" />
          Apply All Pending
        </Button>
        <Button variant="outline" onClick={() => setRevertOpen(true)} disabled={!status?.applied}>
          <ChevronLeft className="mr-1.5 h-4 w-4" />
          Revert Last
        </Button>
        <Button variant="outline" onClick={() => setSnapshotOpen(true)}>
          <Plus className="mr-1.5 h-4 w-4" />
          Generate Snapshot
        </Button>
      </div>

      <ConfirmDialog
        open={applyOpen}
        onOpenChange={setApplyOpen}
        title="Apply All Pending Migrations"
        description={`This will apply ${status?.pending ?? 0} pending migration(s) to the database. This action may modify your database schema.`}
        confirmLabel="Apply"
        onConfirm={handleApply}
      />

      <ConfirmDialog
        open={revertOpen}
        onOpenChange={setRevertOpen}
        title="Revert Last Migration"
        description="This will revert the most recently applied migration. This may result in data loss if the migration created tables or columns."
        confirmLabel="Revert"
        variant="destructive"
        onConfirm={handleRevert}
      />

      <ConfirmDialog
        open={snapshotOpen}
        onOpenChange={setSnapshotOpen}
        title="Generate Snapshot"
        description="This will generate a new snapshot migration file capturing the current state of all collections."
        confirmLabel="Generate"
        onConfirm={handleSnapshot}
      />
    </>
  )
}
