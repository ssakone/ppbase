import { useMigrations, useMigrationStatus } from '@/hooks/use-migrations'
import { ContentHeader } from '@/components/content-header'
import { Breadcrumb } from '@/components/breadcrumb'
import { MigrationStatusCards } from '@/components/migration-status-cards'
import { MigrationActions } from '@/components/migration-actions'
import { MigrationsTable } from '@/components/migrations-table'
import { LoadingSpinner } from '@/components/loading-spinner'
import { EmptyState } from '@/components/empty-state'
import { Button } from '@/components/ui/button'
import { FileText, AlertCircle } from 'lucide-react'

export function MigrationsPage() {
  const { data: migrationsData, isLoading, isError, refetch } = useMigrations()
  const migrations = migrationsData?.items ?? []
  const { data: status } = useMigrationStatus()

  return (
    <>
      <ContentHeader
        left={<Breadcrumb items={[{ label: 'Migrations', active: true }]} />}
        right={<MigrationActions status={status} />}
      />
      <div className="flex-1 overflow-auto p-6">
        {isLoading ? (
          <LoadingSpinner fullPage />
        ) : isError ? (
          <div className="flex flex-col items-center justify-center py-16 text-center">
            <AlertCircle className="h-10 w-10 text-destructive mb-3" />
            <h3 className="text-lg font-semibold mb-1">Failed to load migrations</h3>
            <p className="text-sm text-muted-foreground mb-4">
              Could not connect to the migrations API.
            </p>
            <Button variant="outline" onClick={() => refetch()}>
              Retry
            </Button>
          </div>
        ) : (
          <div className="space-y-6">
            <MigrationStatusCards status={status} />
            {migrations.length === 0 ? (
              <EmptyState
                icon={<FileText className="h-6 w-6" />}
                title="No migrations"
                description="No migration files found. Generate a snapshot to create your first migration."
              />
            ) : (
              <MigrationsTable migrations={migrations} />
            )}
          </div>
        )}
      </div>
    </>
  )
}
