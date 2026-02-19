import { useState, useCallback, useEffect } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { Settings, RefreshCw, Plus, Code, Database, ChevronLeft, ChevronRight } from 'lucide-react'
import { useCollection } from '@/hooks/use-collections'
import { useRecords, useDeleteRecord } from '@/hooks/use-records'
import { ContentHeader } from '@/components/content-header'
import { Breadcrumb } from '@/components/breadcrumb'
import { RecordsTable } from '@/components/records-table'
import { RecordEditor } from '@/components/record-editor'
import { ApiPreviewDrawer } from '@/components/api-preview-drawer'
import { CollectionEditor } from '@/components/collection-editor'
import { SelectionBar } from '@/components/selection-bar'
import { ConfirmDialog } from '@/components/confirm-dialog'
import { LoadingSpinner } from '@/components/loading-spinner'
import { EmptyState } from '@/components/empty-state'
import { Button } from '@/components/ui/button'

export function RecordsPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()

  const { data: collection, isLoading: isCollectionLoading } = useCollection(id)
  const [page, setPage] = useState(1)
  const [perPage] = useState(30)
  const [filter, setFilter] = useState('')

  const {
    data: records,
    isLoading: isRecordsLoading,
    isError: isRecordsError,
    error: recordsError,
    refetch,
  } = useRecords(id, { page, perPage, filter: filter || undefined })

  // Reset filter and page when switching collections
  useEffect(() => {
    setFilter('')
    setPage(1)
  }, [id])

  const deleteMutation = useDeleteRecord(id ?? '')

  const [isEditorOpen, setIsEditorOpen] = useState(false)
  const [editingRecordId, setEditingRecordId] = useState<string | null>(null)
  const [duplicateData, setDuplicateData] = useState<Record<string, unknown> | null>(null)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [showBulkDeleteConfirm, setShowBulkDeleteConfirm] = useState(false)
  const [isApiPreviewOpen, setIsApiPreviewOpen] = useState(false)
  const [isCollectionEditorOpen, setIsCollectionEditorOpen] = useState(false)

  const isView = collection?.type === 'view'

  // Listen for duplicate events from record editor
  useEffect(() => {
    const handler = (e: Event) => {
      const data = (e as CustomEvent).detail as Record<string, unknown>
      setDuplicateData(data)
      setEditingRecordId(null)
      setIsEditorOpen(true)
    }
    window.addEventListener('ppbase:duplicate-record', handler)
    return () => window.removeEventListener('ppbase:duplicate-record', handler)
  }, [])

  const handleRowClick = useCallback((recordId: string) => {
    setEditingRecordId(recordId)
    setDuplicateData(null)
    setIsEditorOpen(true)
  }, [])

  const handleNewRecord = useCallback(() => {
    setEditingRecordId(null)
    setDuplicateData(null)
    setIsEditorOpen(true)
  }, [])

  const handleEditorClose = useCallback(() => {
    setIsEditorOpen(false)
    setEditingRecordId(null)
    setDuplicateData(null)
  }, [])

  const handleFilterChange = useCallback((newFilter: string) => {
    setFilter(newFilter)
    setPage(1)
  }, [])

  const handleSelectAll = useCallback(
    (checked: boolean) => {
      if (!records) return
      if (checked) {
        setSelectedIds(new Set(records.items.map((r) => r.id)))
      } else {
        setSelectedIds(new Set())
      }
    },
    [records],
  )

  const handleSelectRow = useCallback((recordId: string, checked: boolean) => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (checked) {
        next.add(recordId)
      } else {
        next.delete(recordId)
      }
      return next
    })
  }, [])

  const handleBulkDelete = async () => {
    const ids = Array.from(selectedIds)
    try {
      for (const recordId of ids) {
        await deleteMutation.mutateAsync(recordId)
      }
      toast.success(`Deleted ${ids.length} record${ids.length !== 1 ? 's' : ''}`)
      setSelectedIds(new Set())
      setShowBulkDeleteConfirm(false)
    } catch (err: unknown) {
      const message =
        err && typeof err === 'object' && 'message' in err
          ? String((err as { message: string }).message)
          : 'Failed to delete records'
      toast.error(message)
    }
  }

  if (isCollectionLoading) {
    return <LoadingSpinner fullPage />
  }

  if (!collection) {
    return (
      <EmptyState
        icon={<Database className="h-6 w-6" />}
        title="Collection not found"
        description="The collection you are looking for does not exist."
        action={{ label: 'Back to collections', onClick: () => navigate('/collections') }}
      />
    )
  }

  return (
    <>
      <ContentHeader
        left={
          <Breadcrumb
            items={[
              { label: 'Collections', onClick: () => navigate('/collections') },
              { label: collection.name, active: true },
            ]}
          >
            <button
              onClick={() => setIsCollectionEditorOpen(true)}
              className="ml-0.5 rounded p-1 text-muted-foreground hover:text-foreground hover:bg-muted"
              title="Collection settings"
            >
              <Settings className="h-4.5 w-4.5" />
            </button>
            <button
              onClick={() => refetch()}
              className="rounded p-1 text-muted-foreground hover:text-foreground hover:bg-muted"
              title="Refresh records"
            >
              <RefreshCw className="h-4.5 w-4.5" />
            </button>
          </Breadcrumb>
        }
        right={
          <>
            <Button variant="outline" size="sm" onClick={() => setIsApiPreviewOpen(true)}>
              <Code className="mr-1 h-3.5 w-3.5" />
              API Preview
            </Button>
            {!isView && (
              <Button size="sm" onClick={handleNewRecord}>
                <Plus className="mr-1 h-3.5 w-3.5" />
                New record
              </Button>
            )}
          </>
        }
      />

      <div className="flex-1 overflow-auto p-4 md:p-6">
        {isRecordsError && recordsError && (
          <div className="mb-4 rounded-md border border-destructive/50 bg-destructive/10 px-4 py-3 text-sm text-destructive">
            {typeof recordsError === 'object' && 'message' in recordsError
              ? String((recordsError as { message: string }).message)
              : 'Failed to load records. Check your connection and try again.'}
          </div>
        )}
        <RecordsTable
          key={collection.id}
          collection={collection}
          records={
            records ?? {
              items: [],
              page: 1,
              perPage,
              totalItems: 0,
              totalPages: 0,
            }
          }
          selectedIds={selectedIds}
          onSelectAll={handleSelectAll}
          onSelectRow={handleSelectRow}
          onRowClick={handleRowClick}
          onPageChange={setPage}
          onFilterChange={handleFilterChange}
          onNewRecord={handleNewRecord}
          hidePagination
          isLoading={isRecordsLoading}
        />
      </div>

      {/* Fixed pagination footer */}
      {records && records.totalItems > 0 && (
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2 border-t bg-white px-4 py-3 md:px-8">
          <span className="text-sm text-muted-foreground">
            {(records.page - 1) * records.perPage + 1}-
            {Math.min(records.page * records.perPage, records.totalItems)} of{' '}
            {records.totalItems} records
          </span>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setPage(records.page - 1)}
              disabled={records.page <= 1}
            >
              <ChevronLeft className="h-4 w-4" />
              Previous
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setPage(records.page + 1)}
              disabled={records.page >= records.totalPages}
            >
              Next
              <ChevronRight className="h-4 w-4" />
            </Button>
          </div>
        </div>
      )}

      {selectedIds.size > 0 && (
        <SelectionBar
          count={selectedIds.size}
          onReset={() => setSelectedIds(new Set())}
          onDelete={() => setShowBulkDeleteConfirm(true)}
        />
      )}

      <RecordEditor
        open={isEditorOpen}
        onClose={handleEditorClose}
        collection={collection}
        recordId={editingRecordId}
        duplicateData={duplicateData}
      />

      <ConfirmDialog
        open={showBulkDeleteConfirm}
        onOpenChange={setShowBulkDeleteConfirm}
        title="Delete selected records"
        description={`Are you sure you want to delete ${selectedIds.size} record${selectedIds.size !== 1 ? 's' : ''}? This action cannot be undone.`}
        confirmLabel="Delete"
        variant="destructive"
        onConfirm={handleBulkDelete}
      />

      <ApiPreviewDrawer
        open={isApiPreviewOpen}
        onClose={() => setIsApiPreviewOpen(false)}
        collection={collection}
      />

      <CollectionEditor
        open={isCollectionEditorOpen}
        onClose={() => setIsCollectionEditorOpen(false)}
        onDelete={() => navigate('/collections')}
        mode="edit"
        collectionId={collection.id}
      />
    </>
  )
}
