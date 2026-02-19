import { Collection } from '@/api/types'
import { formatDate } from '@/lib/utils'
import { TypeBadge } from '@/components/type-badge'
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from '@/components/ui/table'
import { Button } from '@/components/ui/button'
import { ArrowRight } from 'lucide-react'

interface CollectionsTableProps {
  collections: Collection[]
  onRowClick: (id: string) => void
}

export function CollectionsTable({ collections, onRowClick }: CollectionsTableProps) {
  return (
    <div className="rounded-xl border shadow-sm overflow-x-auto">
      <Table className="min-w-[500px]">
        <TableHeader>
          <TableRow>
            <TableHead>Name</TableHead>
            <TableHead>Type</TableHead>
            <TableHead>Fields</TableHead>
            <TableHead>Created</TableHead>
            <TableHead className="w-[50px]" />
          </TableRow>
        </TableHeader>
        <TableBody>
          {collections.map((col) => {
            const fields = col.fields || col.schema || []
            return (
              <TableRow
                key={col.id}
                className="cursor-pointer"
                onClick={() => onRowClick(col.id)}
              >
                <TableCell className="font-medium">{col.name}</TableCell>
                <TableCell>
                  <TypeBadge type={col.type} />
                </TableCell>
                <TableCell className="text-muted-foreground">
                  {fields.length} field{fields.length !== 1 ? 's' : ''}
                </TableCell>
                <TableCell className="text-muted-foreground text-sm">
                  {formatDate(col.created)}
                </TableCell>
                <TableCell>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-7 w-7 p-0"
                    onClick={(e) => {
                      e.stopPropagation()
                      onRowClick(col.id)
                    }}
                  >
                    <ArrowRight className="h-4 w-4" />
                  </Button>
                </TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    </div>
  )
}
