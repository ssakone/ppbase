import { useState, useEffect } from 'react'
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
  const [page, setPage] = useState(1)
  const [perPage] = useState(30)
  const { data: migrationsData, isLoading, isError, refetch } = useMigrations({ page, perPage })
  const migrations = migrationsData?.items ?? []
  const totalPages = migrationsData?.totalPages ?? 1
  const totalItems = migrationsData?.totalItems ?? 0
  const { data: status } = useMigrationStatus()

  useEffect(() => {
    if (page > totalPages) {
      setPage(totalPages)
    }
  }, [page, totalPages])

  return (
    <>
      <ContentHeader
        left={<Breadcrumb items={[{ label: 'Migrations', active: true }]} />}
        right={<MigrationActions status={status} />}
      />
      <div className="flex-1 overflow-auto p-4 md:p-6">
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
            {totalItems === 0 ? (
              <EmptyState
                icon={<FileText className="h-6 w-6" />}
                title="No migrations"
                description="No migration files found. Generate a snapshot to create your first migration."
              />
            ) : (
              <>
                <MigrationsTable migrations={migrations} />

                <div className="flex items-center justify-between text-sm text-muted-foreground">
                  <span>
                    Page {page} of {totalPages} — {totalItems} total migrations
                  </span>
                  <div className="flex gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={page <= 1}
                      onClick={() => setPage((prev) => prev - 1)}
                    >
                      Previous
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={page >= totalPages}
                      onClick={() => setPage((prev) => prev + 1)}
                    >
                      Next
                    </Button>
                  </div>
                </div>
              </>
            )}
          </div>
        )}
      </div>
    </>
  )
}
