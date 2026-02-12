import { useState, useEffect } from 'react'
import { Field } from '@/api/types'
import {
  useCollection,
  useCreateCollection,
  useUpdateCollection,
  useDeleteCollection,
} from '@/hooks/use-collections'
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetFooter,
} from '@/components/ui/sheet'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { TypeBadge } from '@/components/type-badge'
import { ConfirmDialog } from '@/components/confirm-dialog'
import { CollectionTabs } from '@/components/collection-tabs'
import { LoadingSpinner } from '@/components/loading-spinner'
import { MoreVertical, Trash2 } from 'lucide-react'
import { toast } from 'sonner'

interface CollectionEditorProps {
  open: boolean
  onClose: () => void
  onDelete?: () => void
  mode: 'create' | 'edit'
  collectionId?: string
}

export function CollectionEditor({ open, onClose, onDelete, mode, collectionId }: CollectionEditorProps) {
  const { data: existingCollection, isLoading: isLoadingCollection } = useCollection(
    mode === 'edit' ? collectionId : undefined,
  )
  const createMutation = useCreateCollection()
  const updateMutation = useUpdateCollection()
  const deleteMutation = useDeleteCollection()

  const [name, setName] = useState('')
  const [type, setType] = useState<'base' | 'auth' | 'view'>('base')
  const [fields, setFields] = useState<Field[]>([])
  const [viewQuery, setViewQuery] = useState('')
  const [rules, setRules] = useState({
    listRule: '',
    viewRule: '',
    createRule: '',
    updateRule: '',
    deleteRule: '',
  })
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false)

  // Populate form when editing
  useEffect(() => {
    if (mode === 'edit' && existingCollection) {
      setName(existingCollection.name)
      setType(existingCollection.type)
      setFields(existingCollection.fields || existingCollection.schema || [])
      setRules({
        listRule: existingCollection.listRule ?? '',
        viewRule: existingCollection.viewRule ?? '',
        createRule: existingCollection.createRule ?? '',
        updateRule: existingCollection.updateRule ?? '',
        deleteRule: existingCollection.deleteRule ?? '',
      })
      if (existingCollection.type === 'view' && existingCollection.options?.query) {
        setViewQuery(existingCollection.options.query as string)
      }
    }
  }, [mode, existingCollection])

  // Reset form when opening in create mode
  useEffect(() => {
    if (mode === 'create' && open) {
      setName('')
      setType('base')
      setFields([])
      setViewQuery('')
      setRules({ listRule: '', viewRule: '', createRule: '', updateRule: '', deleteRule: '' })
    }
  }, [mode, open])

  const validate = (): boolean => {
    if (!name.trim()) {
      toast.error('Please enter a collection name.')
      return false
    }
    if (type === 'view' && !viewQuery.trim()) {
      toast.error('Please enter a view query.')
      return false
    }
    if (type !== 'view') {
      for (const field of fields) {
        if (field.type === 'select') {
          const vals = field.values || field.options?.values || []
          if (vals.length === 0) {
            toast.error(`Select field "${field.name}" must have at least one value.`)
            return false
          }
        }
        if (field.type === 'relation') {
          const cid = field.collectionId || field.options?.collectionId
          if (!cid) {
            toast.error(`Relation field "${field.name}" must have a target collection.`)
            return false
          }
        }
      }
    }
    return true
  }

  const buildPayload = (): Record<string, unknown> => {
    const payload: Record<string, unknown> = { name: name.trim(), type }

    if (type === 'view') {
      payload.options = { query: viewQuery.trim() }
    } else {
      // Filter out empty-named fields
      const schema = fields.filter((f) => f.name.trim())
      payload.schema = schema
    }

    // Rules: empty input = "" (public); non-empty = filter expression
    const rule = (s: string) => (s.trim() === '' ? '' : s.trim())
    payload.listRule = rule(rules.listRule)
    payload.viewRule = rule(rules.viewRule)
    payload.createRule = rule(rules.createRule)
    payload.updateRule = rule(rules.updateRule)
    payload.deleteRule = rule(rules.deleteRule)

    return payload
  }

  const handleSave = async () => {
    if (!validate()) return

    const payload = buildPayload()

    try {
      if (mode === 'create') {
        await createMutation.mutateAsync(payload)
        toast.success(`Collection "${name}" created.`)
      } else {
        await updateMutation.mutateAsync({
          idOrName: collectionId!,
          data: payload,
        })
        toast.success(`Collection "${name}" updated.`)
      }
      onClose()
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Operation failed.'
      toast.error(msg)
    }
  }

  const handleDelete = async () => {
    if (!collectionId) return
    try {
      await deleteMutation.mutateAsync(collectionId)
      toast.success(`Collection "${name}" deleted.`)
      setDeleteDialogOpen(false)
      onDelete?.()
      onClose()
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to delete collection.'
      toast.error(msg)
    }
  }

  const isSaving = createMutation.isPending || updateMutation.isPending
  const isSystem = existingCollection?.system === true

  return (
    <>
      <Sheet open={open} onOpenChange={(o) => !o && onClose()}>
        <SheetContent side="right" className="w-full sm:max-w-[700px] flex flex-col overflow-hidden">
          <SheetHeader className="shrink-0 px-6 pt-6 pb-4 flex flex-row items-center justify-between pr-8">
            <SheetTitle>{mode === 'create' ? 'New collection' : 'Edit collection'}</SheetTitle>
            {mode === 'edit' && !isSystem && (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button variant="ghost" size="sm" className="h-8 w-8 p-0">
                    <MoreVertical className="h-4 w-4" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  <DropdownMenuItem
                    className="text-destructive focus:text-destructive"
                    onClick={() => setDeleteDialogOpen(true)}
                  >
                    <Trash2 className="h-4 w-4 mr-2" />
                    Delete collection
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            )}
          </SheetHeader>

          <div className="border-t shrink-0" />

          {mode === 'edit' && isLoadingCollection ? (
            <LoadingSpinner fullPage />
          ) : (
            <>
              <div className="flex-1 overflow-y-auto px-6 space-y-4 py-4">
                <div className="space-y-1.5">
                  <Label>
                    Name <span className="text-muted-foreground">*</span>
                  </Label>
                  <div className="flex items-center gap-3">
                    <Input
                      className="flex-1"
                      placeholder='e.g. "posts"'
                      value={name}
                      onChange={(e) => setName(e.target.value)}
                      disabled={isSystem}
                    />
                    {mode === 'create' ? (
                      <Select
                        value={type}
                        onValueChange={(v) => setType(v as 'base' | 'auth' | 'view')}
                      >
                        <SelectTrigger className="w-[140px]">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="base">Type: Base</SelectItem>
                          <SelectItem value="auth">Type: Auth</SelectItem>
                          <SelectItem value="view">Type: View</SelectItem>
                        </SelectContent>
                      </Select>
                    ) : (
                      <TypeBadge type={type} />
                    )}
                  </div>
                </div>

                <CollectionTabs
                  type={type}
                  fields={fields}
                  setFields={setFields}
                  rules={rules}
                  setRules={setRules}
                  viewQuery={viewQuery}
                  setViewQuery={setViewQuery}
                />
              </div>

              <div className="border-t shrink-0" />

              <SheetFooter className="shrink-0 px-6 py-2.5">
                <Button variant="outline" onClick={onClose} disabled={isSaving}>
                  Cancel
                </Button>
                {!isSystem && (
                  <Button onClick={handleSave} disabled={isSaving}>
                    {isSaving && <LoadingSpinner size="sm" className="mr-2" />}
                    {mode === 'create' ? 'Create' : 'Save changes'}
                  </Button>
                )}
              </SheetFooter>
            </>
          )}
        </SheetContent>
      </Sheet>

      <ConfirmDialog
        open={deleteDialogOpen}
        onOpenChange={setDeleteDialogOpen}
        title="Delete Collection"
        description={`Are you sure you want to delete the collection "${name}"? This action cannot be undone and all associated records will be permanently deleted.`}
        confirmLabel="Delete permanently"
        variant="destructive"
        onConfirm={handleDelete}
      />
    </>
  )
}
