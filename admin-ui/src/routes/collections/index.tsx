import { useState, useCallback, useEffect } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useCollections } from '@/hooks/use-collections'
import { ContentHeader } from '@/components/content-header'
import { Breadcrumb } from '@/components/breadcrumb'
import { CollectionsTable } from '@/components/collections-table'
import { CollectionEditor } from '@/components/collection-editor'
import { Button } from '@/components/ui/button'
import { LoadingSpinner } from '@/components/loading-spinner'
import { EmptyState } from '@/components/empty-state'
import { Plus, LayoutGrid } from 'lucide-react'

export function CollectionsPage() {
  const { data: collections = [], isLoading } = useCollections()
  const [searchParams, setSearchParams] = useSearchParams()
  const navigate = useNavigate()
  const editId = searchParams.get('edit')
  const [editorOpen, setEditorOpen] = useState(searchParams.get('new') === '1' || !!editId)
  const [editingCollectionId, setEditingCollectionId] = useState<string | null>(editId)

  useEffect(() => {
    const id = searchParams.get('edit')
    if (id) {
      setEditingCollectionId(id)
      setEditorOpen(true)
    } else if (searchParams.get('new') === '1') {
      setEditingCollectionId(null)
      setEditorOpen(true)
    }
  }, [searchParams])

  const handleNewCollection = useCallback(() => {
    setEditingCollectionId(null)
    setEditorOpen(true)
    setSearchParams({})
  }, [setSearchParams])

  const handleEditorClose = useCallback(() => {
    setEditorOpen(false)
    setEditingCollectionId(null)
    setSearchParams({})
  }, [setSearchParams])

  const handleCollectionClick = useCallback(
    (id: string) => {
      navigate(`/collections/${id}`)
    },
    [navigate],
  )

  return (
    <>
      <ContentHeader
        left={<Breadcrumb items={[{ label: 'Collections', active: true }]} />}
        right={
          <Button onClick={handleNewCollection}>
            <Plus className="h-4 w-4" />
            New collection
          </Button>
        }
      />
      <div className="flex-1 overflow-auto p-4 md:p-6">
        {isLoading ? (
          <LoadingSpinner fullPage />
        ) : collections.length === 0 ? (
          <EmptyState
            icon={<LayoutGrid className="h-6 w-6" />}
            title="No collections yet"
            description="Create your first collection to start organizing your data."
            action={{ label: 'New collection', onClick: handleNewCollection }}
          />
        ) : (
          <CollectionsTable
            collections={collections}
            onRowClick={handleCollectionClick}
          />
        )}
      </div>

      <CollectionEditor
        open={editorOpen}
        onClose={handleEditorClose}
        mode={editingCollectionId ? 'edit' : 'create'}
        collectionId={editingCollectionId ?? undefined}
      />
    </>
  )
}
