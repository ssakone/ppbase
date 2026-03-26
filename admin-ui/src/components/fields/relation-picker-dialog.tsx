import { useMemo, useState, useEffect } from 'react'
import { Search } from 'lucide-react'
import { useCollection } from '@/hooks/use-collections'
import { useRecords } from '@/hooks/use-records'
import {
  normalizeSearchFilter,
  getSearchableFields,
  type SearchableField,
} from '@/lib/normalize-search-filter'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { LoadingSpinner } from '@/components/loading-spinner'
import type { Collection, RecordModel } from '@/api/types'
import { getRelationDisplayValue } from '@/lib/format-cell'
import { truncateId } from '@/lib/utils'

interface RelationPickerDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  targetCollectionId?: string
  collections?: Collection[]
  isMulti: boolean
  selectedIds: string[]
  onConfirm: (selectedIds: string[]) => void
}

function getCollectionName(collectionId: string | undefined, collections?: Collection[]): string {
  if (!collectionId) return 'unknown'
  return collections?.find((collection) => collection.id === collectionId)?.name ?? collectionId
}

export function RelationPickerDialog({
  open,
  onOpenChange,
  targetCollectionId,
  collections,
  isMulti,
  selectedIds,
  onConfirm,
}: RelationPickerDialogProps) {
  const [page, setPage] = useState(1)
  const [perPage] = useState(15)
  const [searchInput, setSearchInput] = useState('')
  const [filter, setFilter] = useState('')
  const [localSelected, setLocalSelected] = useState<Set<string>>(new Set(selectedIds))

  useEffect(() => {
    if (open) {
      setLocalSelected(new Set(selectedIds))
      setPage(1)
      setSearchInput('')
      setFilter('')
    }
  }, [open, selectedIds])

  const { data: targetCollection } = useCollection(targetCollectionId)

  const searchableFields = useMemo<SearchableField[]>(
    () => (targetCollection ? getSearchableFields(targetCollection) : [{ name: 'id', op: '~' }]),
    [targetCollection],
  )

  const { data: records, isLoading } = useRecords(targetCollectionId, {
    page,
    perPage,
    filter: filter || undefined,
  })

  const collectionName = getCollectionName(targetCollectionId, collections)

  const handleSearchSubmit = () => {
    const normalized = normalizeSearchFilter(searchInput, searchableFields)
    setFilter(normalized)
    setPage(1)
  }

  const handleSearchReset = () => {
    setSearchInput('')
    setFilter('')
    setPage(1)
  }

  const handleRowClick = (recordId: string) => {
    if (!isMulti) {
      onConfirm([recordId])
      onOpenChange(false)
      return
    }

    setLocalSelected((prev) => {
      const next = new Set(prev)
      if (next.has(recordId)) {
        next.delete(recordId)
      } else {
        next.add(recordId)
      }
      return next
    })
  }

  const handleApply = () => {
    onConfirm(Array.from(localSelected))
    onOpenChange(false)
  }

  const items = records?.items ?? []
  const totalPages = records?.totalPages ?? 1
  const hasTargetCollection = !!targetCollectionId

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>Select related record{isMulti ? 's' : ''}</DialogTitle>
          <DialogDescription>
            Target collection: {collectionName}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="flex items-center gap-2">
            <div className="relative flex-1">
              <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                type="text"
                placeholder='Search or filter (ex: name ~ "john")...'
                value={searchInput}
                onChange={(e) => setSearchInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault()
                    handleSearchSubmit()
                  }
                }}
                className="pl-9"
                disabled={!hasTargetCollection}
              />
            </div>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={handleSearchSubmit}
              disabled={!hasTargetCollection}
            >
              Search
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={handleSearchReset}
              disabled={!searchInput && !filter}
            >
              Reset
            </Button>
          </div>

          <p className="text-xs text-muted-foreground">
            Tip: plain text searches across fields, full expressions are used as raw PocketBase filters.
          </p>

          <div className="rounded-md border overflow-x-auto max-h-[380px] overflow-y-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  {isMulti && <TableHead className="w-10" />}
                  <TableHead>ID</TableHead>
                  <TableHead>Display</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {!hasTargetCollection ? (
                  <TableRow>
                    <TableCell colSpan={isMulti ? 3 : 2} className="h-20 text-center text-sm text-muted-foreground">
                      No target collection configured for this relation field.
                    </TableCell>
                  </TableRow>
                ) : isLoading ? (
                  <TableRow>
                    <TableCell colSpan={isMulti ? 3 : 2} className="h-20 text-center">
                      <div className="inline-flex items-center gap-2 text-sm text-muted-foreground">
                        <LoadingSpinner size="sm" />
                        Loading records...
                      </div>
                    </TableCell>
                  </TableRow>
                ) : items.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={isMulti ? 3 : 2} className="h-20 text-center text-sm text-muted-foreground">
                      No records found.
                    </TableCell>
                  </TableRow>
                ) : (
                  items.map((record: RecordModel) => {
                    const display = getRelationDisplayValue(record) ?? truncateId(record.id)
                    const checked = localSelected.has(record.id)

                    return (
                      <TableRow
                        key={record.id}
                        className={`cursor-pointer hover:bg-muted/40 ${checked ? 'bg-slate-100/70' : ''}`}
                        onClick={() => handleRowClick(record.id)}
                      >
                        {isMulti && (
                          <TableCell onClick={(e) => e.stopPropagation()}>
                            <Checkbox
                              checked={checked}
                              onCheckedChange={() => handleRowClick(record.id)}
                            />
                          </TableCell>
                        )}
                        <TableCell className="font-mono text-xs text-muted-foreground">{truncateId(record.id)}</TableCell>
                        <TableCell className="max-w-[420px] truncate">{display}</TableCell>
                      </TableRow>
                    )
                  })
                )}
              </TableBody>
            </Table>
          </div>

          <div className="flex items-center justify-between text-sm text-muted-foreground">
            <span>
              {isMulti ? `${localSelected.size} selected` : 'Click a row to select'}
            </span>
            <div className="flex items-center gap-2">
              <span>
                Page {records?.page ?? page} / {totalPages}
              </span>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setPage((prev) => Math.max(1, prev - 1))}
                disabled={page <= 1}
              >
                Previous
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setPage((prev) => prev + 1)}
                disabled={page >= totalPages}
              >
                Next
              </Button>
            </div>
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          {isMulti && (
            <Button onClick={handleApply}>
              Apply selection ({localSelected.size})
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
