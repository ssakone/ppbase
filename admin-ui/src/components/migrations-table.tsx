import type { Migration } from '@/api/types'
import { formatDate } from '@/lib/utils'
import { Badge } from '@/components/ui/badge'
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
  TableFooter,
} from '@/components/ui/table'

interface MigrationsTableProps {
  migrations: Migration[]
}

export function MigrationsTable({ migrations }: MigrationsTableProps) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>File</TableHead>
          <TableHead>Status</TableHead>
          <TableHead>Applied At</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {migrations.map((migration) => (
          <TableRow key={migration.file}>
            <TableCell className="font-medium font-mono text-sm">
              {migration.file}
            </TableCell>
            <TableCell>
              {migration.status === 'applied' ? (
                <Badge className="bg-green-100 text-green-800 border-green-200 hover:bg-green-100">
                  Applied
                </Badge>
              ) : (
                <Badge className="bg-amber-100 text-amber-800 border-amber-200 hover:bg-amber-100">
                  Pending
                </Badge>
              )}
            </TableCell>
            <TableCell className="text-muted-foreground">
              {formatDate(migration.applied)}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
      <TableFooter>
        <TableRow>
          <TableCell colSpan={3} className="text-muted-foreground">
            Showing {migrations.length} migration{migrations.length !== 1 ? 's' : ''} on this page
          </TableCell>
        </TableRow>
      </TableFooter>
    </Table>
  )
}
