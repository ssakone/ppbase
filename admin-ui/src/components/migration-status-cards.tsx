import type { MigrationStatus } from '@/api/types'
import { formatDate } from '@/lib/utils'

interface MigrationStatusCardsProps {
  status: MigrationStatus | undefined
}

export function MigrationStatusCards({ status }: MigrationStatusCardsProps) {
  return (
    <div className="space-y-3">
      <div className="flex gap-4">
        <div className="flex-1 rounded-lg border bg-white p-4">
          <div className="text-2xl font-bold">{status?.total ?? 0}</div>
          <div className="text-sm text-muted-foreground">Total</div>
        </div>
        <div className="flex-1 rounded-lg border border-green-200 bg-green-50 p-4">
          <div className="text-2xl font-bold text-green-700">{status?.applied ?? 0}</div>
          <div className="text-sm text-green-600">Applied</div>
        </div>
        <div className="flex-1 rounded-lg border border-amber-200 bg-amber-50 p-4">
          <div className="text-2xl font-bold text-amber-700">{status?.pending ?? 0}</div>
          <div className="text-sm text-amber-600">Pending</div>
        </div>
      </div>
      {status?.lastApplied && (
        <p className="text-xs text-muted-foreground">
          Last applied: {formatDate(status.lastApplied)}
        </p>
      )}
    </div>
  )
}
