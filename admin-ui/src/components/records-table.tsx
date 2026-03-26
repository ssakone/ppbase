import { useState, useEffect, useCallback, useMemo } from 'react'
import { Search, ChevronLeft, ChevronRight, Plus, MoreHorizontal, ExternalLink } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Checkbox } from '@/components/ui/checkbox'
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuCheckboxItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
} from '@/components/ui/dropdown-menu'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import type { Collection, Field, RecordModel, PaginatedResult } from '@/api/types'
import {
  formatCellValue,
  getRelationDisplayValues,
  getRelationRecordIds,
} from '@/lib/format-cell'
import { formatDate, truncateId } from '@/lib/utils'
import { ImagePreview } from '@/components/ui/image-preview'
import { FieldTypeBadge } from '@/components/field-type-badge'
import { getSystemColumnType } from '@/lib/field-types'
import { normalizeSearchFilter, getSearchableFields } from '@/lib/normalize-search-filter'

interface RecordsTableProps {
  collection: Collection
  records: PaginatedResult<RecordModel>
  selectedIds: Set<string>
  onSelectAll: (checked: boolean) => void
  onSelectRow: (id: string, checked: boolean) => void
  onRowClick: (id: string) => void
  onPageChange: (page: number) => void
  onFilterChange: (filter: string) => void
  onNewRecord?: () => void
  hidePagination?: boolean
  isLoading?: boolean
}

// System columns that can be toggled
const SYSTEM_COLUMNS = ['id', 'created', 'updated'] as const

// Auth system fields are returned in records API payloads but are not part of
// collection.schema/fields, so include them explicitly in the records table.
const AUTH_SYSTEM_FIELDS: Field[] = [
  { name: 'email', type: 'email' },
  { name: 'emailVisibility', type: 'bool' },
  { name: 'verified', type: 'bool' },
]

function getSchemaFields(collection: Collection, records: RecordModel[]): Field[] {
  const attachAuthSystemFields = (base: Field[]): Field[] => {
    if (collection.type !== 'auth') return base
    const existing = new Set(base.map((f) => f.name))
    const authFields = AUTH_SYSTEM_FIELDS.filter((f) => !existing.has(f.name))
    return [...authFields, ...base]
  }

  const fields = collection.fields ?? collection.schema ?? []
  if (fields.length > 0) return attachAuthSystemFields(fields)

  // For view collections with empty schema, infer from first record
  if (records.length > 0) {
    const systemKeys = new Set(['id', 'collectionId', 'collectionName', 'created', 'updated'])
    const keys = Object.keys(records[0]).filter((k) => !systemKeys.has(k))
    return attachAuthSystemFields(keys.map((k) => ({ name: k, type: 'text' })))
  }

  return attachAuthSystemFields([])
}

function getRelationCollectionId(field: Field): string | undefined {
  return field.collectionId ?? field.options?.collectionId
}

const STORAGE_PREFIX = 'ppbase_columns_'

function getStoredVisibility(collectionId: string): Set<string> | null {
  try {
    const raw = localStorage.getItem(STORAGE_PREFIX + collectionId)
    if (!raw) return null
    const arr = JSON.parse(raw)
    if (Array.isArray(arr)) return new Set(arr as string[])
  } catch { /* ignore */ }
  return null
}

function storeVisibility(collectionId: string, hidden: Set<string>) {
  localStorage.setItem(STORAGE_PREFIX + collectionId, JSON.stringify([...hidden]))
}

export function RecordsTable({
  collection,
  records,
  selectedIds,
  onSelectAll,
  onSelectRow,
  onRowClick,
  onPageChange,
  onFilterChange,
  onNewRecord,
  hidePagination,
  isLoading,
}: RecordsTableProps) {
  const [searchInput, setSearchInput] = useState('')
  const isView = collection.type === 'view'
  const fields = getSchemaFields(collection, records.items)
  const allSelected =
    records.items.length > 0 && records.items.every((r) => selectedIds.has(r.id))

  const startItem = (records.page - 1) * records.perPage + 1
  const endItem = Math.min(records.page * records.perPage, records.totalItems)

  // Hidden columns state — persisted per collection
  const [hiddenColumns, setHiddenColumns] = useState<Set<string>>(() => {
    return getStoredVisibility(collection.id) ?? new Set()
  })

  // Reset hidden columns when collection changes
  useEffect(() => {
    const stored = getStoredVisibility(collection.id)
    setHiddenColumns(stored ?? new Set())
  }, [collection.id])

  const toggleColumn = useCallback(
    (colName: string) => {
      setHiddenColumns((prev) => {
        const next = new Set(prev)
        if (next.has(colName)) {
          next.delete(colName)
        } else {
          next.add(colName)
        }
        storeVisibility(collection.id, next)
        return next
      })
    },
    [collection.id],
  )

  const showAll = useCallback(() => {
    setHiddenColumns(new Set())
    storeVisibility(collection.id, new Set())
  }, [collection.id])

  const visibleFields = useMemo(
    () => fields.filter((f) => !hiddenColumns.has(f.name)),
    [fields, hiddenColumns],
  )

  const showId = !hiddenColumns.has('id')
  const showCreated = !hiddenColumns.has('created')
  const showUpdated = !hiddenColumns.has('updated')

  const hiddenCount = hiddenColumns.size

  // Searchable fields for plain-text → filter conversion (PocketBase-style)
  const searchableFields = useMemo(
    () => getSearchableFields(collection),
    [collection],
  )

  const handleSearchSubmit = useCallback(() => {
    const normalized = normalizeSearchFilter(searchInput, searchableFields)
    onFilterChange(normalized)
  }, [searchInput, onFilterChange, searchableFields])

  const openRelatedRecord = useCallback((e: React.MouseEvent, targetCollectionId: string, targetRecordId: string) => {
    e.stopPropagation()
    const targetUrl = `${window.location.origin}/_/collections/${encodeURIComponent(targetCollectionId)}?record=${encodeURIComponent(targetRecordId)}`
    window.open(targetUrl, '_blank', 'noopener,noreferrer')
  }, [])

  const renderRelationCell = useCallback((record: RecordModel, field: Field) => {
    const targetCollectionId = getRelationCollectionId(field)
    if (!targetCollectionId) {
      return formatCellValue(record[field.name], field)
    }

    const labels = getRelationDisplayValues(record, field.name)
    const relationIds = getRelationRecordIds(record, field.name)

    if (labels.length === 0 || relationIds.length === 0) {
      return formatCellValue(record[field.name], field)
    }

    return (
      <div className="flex flex-wrap items-center gap-1.5" title="Open related record in new tab">
        {labels.map((label, idx) => {
          const recordId = relationIds[idx]
          if (!recordId) {
            return (
              <span key={`${field.name}-${idx}`} className="text-xs text-muted-foreground">
                {label}
              </span>
            )
          }

          return (
            <button
              key={`${field.name}-${recordId}-${idx}`}
              type="button"
              className="inline-flex max-w-full items-center gap-1 rounded-md border border-slate-200 px-2 py-0.5 text-xs text-slate-700 hover:border-slate-300 hover:bg-slate-50"
              onClick={(e) => openRelatedRecord(e, targetCollectionId, recordId)}
              title={`Open related record ${recordId}`}
            >
              <span className="truncate">{label}</span>
              <ExternalLink className="h-3 w-3 shrink-0" />
            </button>
          )
        })}
      </div>
    )
  }, [openRelatedRecord])

  // +1 for the column-toggle header at the end
  const visibleColCount =
    (isView ? 0 : 1) + (showId ? 1 : 0) + visibleFields.length + (showCreated ? 1 : 0) + (showUpdated ? 1 : 0) + 1

  return (
    <div className="space-y-0">
      {/* Search bar */}
      <div className="pb-4">
        <div className="relative">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            type="text"
            placeholder={`Search term or filter like created > "2022-01-01"...`}
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault()
                handleSearchSubmit()
              }
            }}
            className="pl-9"
          />
        </div>
      </div>

      {/* Table */}
      <div className="rounded-md border overflow-x-auto">
        <Table className="min-w-max">
          <TableHeader>
            <TableRow className="bg-slate-50/80">
              {!isView && (
                <TableHead className="w-10">
                  <Checkbox
                    checked={allSelected}
                    onCheckedChange={(checked) => onSelectAll(!!checked)}
                  />
                </TableHead>
              )}
              {showId && (
                <TableHead className="w-32">
                  <span className="inline-flex items-center gap-1.5">
                    <FieldTypeBadge type={getSystemColumnType('id')} />
                    <span>id</span>
                  </span>
                </TableHead>
              )}
              {visibleFields.map((f) => (
                <TableHead key={f.name}>
                  <span className="inline-flex items-center gap-1.5">
                    <FieldTypeBadge type={f.type} />
                    <span>{f.name}</span>
                  </span>
                </TableHead>
              ))}
              {showCreated && (
                <TableHead className="w-40">
                  <span className="inline-flex items-center gap-1.5">
                    <FieldTypeBadge type={getSystemColumnType('created')} />
                    <span>created</span>
                  </span>
                </TableHead>
              )}
              {showUpdated && (
                <TableHead className="w-40">
                  <span className="inline-flex items-center gap-1.5">
                    <FieldTypeBadge type={getSystemColumnType('updated')} />
                    <span>updated</span>
                  </span>
                </TableHead>
              )}
              {/* Column toggle — last header cell like PocketBase's "..." */}
              <TableHead className="w-10 text-right">
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <button
                      className="inline-flex items-center justify-center h-7 w-7 rounded hover:bg-slate-200 text-muted-foreground hover:text-foreground transition-colors relative"
                    >
                      <MoreHorizontal className="h-4 w-4" />
                      {hiddenCount > 0 && (
                        <span className="absolute -top-0.5 -right-0.5 flex items-center justify-center rounded-full bg-indigo-500 text-white text-[9px] font-bold h-3.5 min-w-3.5 px-0.5">
                          {hiddenCount}
                        </span>
                      )}
                    </button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end" className="w-52 max-h-80 overflow-y-auto">
                    <DropdownMenuLabel className="flex items-center justify-between">
                      <span>Toggle columns</span>
                      {hiddenCount > 0 && (
                        <button
                          onClick={showAll}
                          className="text-xs text-indigo-600 hover:underline font-normal"
                        >
                          Show all
                        </button>
                      )}
                    </DropdownMenuLabel>
                    <DropdownMenuSeparator />
                    {/* System columns */}
                    {SYSTEM_COLUMNS.map((col) => (
                      <DropdownMenuCheckboxItem
                        key={col}
                        checked={!hiddenColumns.has(col)}
                        onCheckedChange={() => toggleColumn(col)}
                        onSelect={(e) => e.preventDefault()}
                      >
                        <span className="inline-flex items-center gap-1.5">
                          <FieldTypeBadge type={getSystemColumnType(col)} className="scale-90" />
                          <span>{col}</span>
                        </span>
                      </DropdownMenuCheckboxItem>
                    ))}
                    {fields.length > 0 && <DropdownMenuSeparator />}
                    {/* User fields */}
                    {fields.map((f) => (
                      <DropdownMenuCheckboxItem
                        key={f.name}
                        checked={!hiddenColumns.has(f.name)}
                        onCheckedChange={() => toggleColumn(f.name)}
                        onSelect={(e) => e.preventDefault()}
                      >
                        <span className="inline-flex items-center gap-1.5">
                          <FieldTypeBadge type={f.type} className="scale-90" />
                          <span>{f.name}</span>
                        </span>
                      </DropdownMenuCheckboxItem>
                    ))}
                  </DropdownMenuContent>
                </DropdownMenu>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading ? (
              <TableRow>
                <TableCell
                  colSpan={Math.max(1, visibleColCount)}
                  className="h-24 text-center"
                >
                  <div className="flex items-center justify-center gap-2 py-4 text-muted-foreground text-sm">
                    <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
                    Loading...
                  </div>
                </TableCell>
              </TableRow>
            ) : records.items.length === 0 ? (
              <TableRow>
                <TableCell
                  colSpan={Math.max(1, visibleColCount)}
                  className="h-24 text-center"
                >
                  <div className="flex flex-col items-center gap-2 py-4">
                    <span className="text-muted-foreground text-sm">No records found.</span>
                    {onNewRecord && !isView && (
                      <Button variant="outline" size="sm" onClick={onNewRecord}>
                        <Plus className="mr-1.5 h-3.5 w-3.5" />
                        New record
                      </Button>
                    )}
                  </div>
                </TableCell>
              </TableRow>
            ) : (
              records.items.map((record) => (
                <TableRow
                  key={record.id}
                  className="cursor-pointer hover:bg-muted/50"
                  onClick={() => onRowClick(record.id)}
                >
                  {!isView && (
                    <TableCell onClick={(e) => e.stopPropagation()}>
                      <Checkbox
                        checked={selectedIds.has(record.id)}
                        onCheckedChange={(checked) =>
                          onSelectRow(record.id, !!checked)
                        }
                      />
                    </TableCell>
                  )}
                  {showId && (
                    <TableCell className="font-mono text-xs text-muted-foreground">
                      {truncateId(record.id)}
                    </TableCell>
                  )}
                  {visibleFields.map((f) => (
                    <TableCell key={f.name} className={`max-w-[260px] ${f.type === 'relation' ? 'align-top' : 'truncate'}`}>
                      {f.type === 'file' ? (
                        <ImagePreview
                          collectionId={collection.id}
                          recordId={record.id}
                          files={record[f.name] as string | string[]}
                        />
                      ) : f.type === 'relation' ? (
                        renderRelationCell(record, f)
                      ) : (
                        formatCellValue(record[f.name], f)
                      )}
                    </TableCell>
                  ))}
                  {showCreated && (
                    <TableCell className="text-xs text-muted-foreground">
                      {formatDate(record.created)}
                    </TableCell>
                  )}
                  {showUpdated && (
                    <TableCell className="text-xs text-muted-foreground">
                      {formatDate(record.updated as string)}
                    </TableCell>
                  )}
                  {/* Empty cell for the toggle column */}
                  <TableCell />
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>

      {/* Pagination footer (can be hidden when rendered externally) */}
      {!hidePagination && records.totalItems > 0 && (
        <div className="flex items-center justify-between px-2 pt-4">
          <span className="text-sm text-muted-foreground">
            {startItem}-{endItem} of {records.totalItems} records
          </span>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => onPageChange(records.page - 1)}
              disabled={records.page <= 1}
            >
              <ChevronLeft className="h-4 w-4" />
              Previous
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => onPageChange(records.page + 1)}
              disabled={records.page >= records.totalPages}
            >
              Next
              <ChevronRight className="h-4 w-4" />
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}
