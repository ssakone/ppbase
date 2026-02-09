import { useState, useEffect, useCallback } from 'react'
import { Search, ChevronLeft, ChevronRight } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import type { Collection, Field, RecordModel, PaginatedResult } from '@/api/types'
import { formatCellValue } from '@/lib/format-cell'
import { formatDate, truncateId } from '@/lib/utils'

interface RecordsTableProps {
  collection: Collection
  records: PaginatedResult<RecordModel>
  selectedIds: Set<string>
  onSelectAll: (checked: boolean) => void
  onSelectRow: (id: string, checked: boolean) => void
  onRowClick: (id: string) => void
  onPageChange: (page: number) => void
  onFilterChange: (filter: string) => void
  hidePagination?: boolean
}

function getSchemaFields(collection: Collection, records: RecordModel[]): Field[] {
  const fields = collection.fields ?? collection.schema ?? []
  if (fields.length > 0) return fields.slice(0, 5)

  // For view collections with empty schema, infer from first record
  if (records.length > 0) {
    const systemKeys = new Set(['id', 'collectionId', 'collectionName', 'created', 'updated'])
    const keys = Object.keys(records[0]).filter((k) => !systemKeys.has(k))
    return keys.slice(0, 5).map((k) => ({ name: k, type: 'text' }))
  }

  return []
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
  hidePagination,
}: RecordsTableProps) {
  const [searchInput, setSearchInput] = useState('')
  const isView = collection.type === 'view'
  const fields = getSchemaFields(collection, records.items)
  const allSelected =
    records.items.length > 0 && records.items.every((r) => selectedIds.has(r.id))

  const startItem = (records.page - 1) * records.perPage + 1
  const endItem = Math.min(records.page * records.perPage, records.totalItems)

  // Debounced filter
  const handleSearchChange = useCallback(
    (value: string) => {
      setSearchInput(value)
    },
    [],
  )

  useEffect(() => {
    const timer = setTimeout(() => {
      onFilterChange(searchInput)
    }, 300)
    return () => clearTimeout(timer)
  }, [searchInput, onFilterChange])

  return (
    <div className="space-y-0">
      {/* Search bar */}
      <div className="px-1 pb-4">
        <div className="relative">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            type="text"
            placeholder="Search or filter records..."
            value={searchInput}
            onChange={(e) => handleSearchChange(e.target.value)}
            className="pl-9"
          />
        </div>
      </div>

      {/* Table */}
      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              {!isView && (
                <TableHead className="w-10">
                  <Checkbox
                    checked={allSelected}
                    onCheckedChange={(checked) => onSelectAll(!!checked)}
                  />
                </TableHead>
              )}
              <TableHead className="w-32">ID</TableHead>
              {fields.map((f) => (
                <TableHead key={f.name}>{f.name}</TableHead>
              ))}
              <TableHead className="w-40">Created</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {records.items.length === 0 ? (
              <TableRow>
                <TableCell
                  colSpan={(isView ? 0 : 1) + 1 + fields.length + 1}
                  className="h-24 text-center text-muted-foreground"
                >
                  No records found.
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
                  <TableCell className="font-mono text-xs text-muted-foreground">
                    {truncateId(record.id)}
                  </TableCell>
                  {fields.map((f) => (
                    <TableCell key={f.name} className="max-w-[200px] truncate">
                      {formatCellValue(record[f.name], f)}
                    </TableCell>
                  ))}
                  <TableCell className="text-xs text-muted-foreground">
                    {formatDate(record.created)}
                  </TableCell>
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
