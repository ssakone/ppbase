import { useState } from 'react'
import type { LogRecord } from '@/api/types'
import { useLog } from '@/hooks/use-logs'
import { cn } from '@/lib/utils'
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { Badge } from '@/components/ui/badge'
import { LoadingSpinner } from '@/components/loading-spinner'
import { Activity } from 'lucide-react'

const METHOD_COLORS: Record<string, string> = {
  GET: 'bg-blue-100 text-blue-700 border-blue-200',
  POST: 'bg-green-100 text-green-700 border-green-200',
  PATCH: 'bg-amber-100 text-amber-700 border-amber-200',
  PUT: 'bg-amber-100 text-amber-700 border-amber-200',
  DELETE: 'bg-red-100 text-red-700 border-red-200',
}

function statusColor(status: number): string {
  if (status >= 500) return 'bg-red-100 text-red-700 border-red-200'
  if (status >= 400) return 'bg-amber-100 text-amber-700 border-amber-200'
  if (status >= 300) return 'bg-blue-100 text-blue-700 border-blue-200'
  return 'bg-green-100 text-green-700 border-green-200'
}

function formatDate(iso: string) {
  return new Date(iso).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

interface LogDetailSheetProps {
  logId: string | null
  onClose: () => void
}

function LogDetailSheet({ logId, onClose }: LogDetailSheetProps) {
  const { data: log, isLoading } = useLog(logId ?? undefined)

  return (
    <Sheet open={!!logId} onOpenChange={(o) => !o && onClose()}>
      <SheetContent className="sm:max-w-[600px] overflow-y-auto">
        <SheetHeader className="pb-4">
          <SheetTitle>Request Details</SheetTitle>
        </SheetHeader>
        {isLoading ? (
          <LoadingSpinner />
        ) : log ? (
          <div className="space-y-4 text-sm">
            <div className="flex flex-wrap gap-2 items-center">
              <Badge className={cn('font-mono text-xs border', METHOD_COLORS[log.method] ?? 'bg-slate-100 text-slate-700 border-slate-200')}>
                {log.method}
              </Badge>
              <Badge className={cn('font-mono text-xs border', statusColor(log.status))}>
                {log.status}
              </Badge>
              <span className="text-muted-foreground">{log.execTime} ms</span>
            </div>
            <div className="rounded-md bg-muted p-3 font-mono text-xs break-all">{log.url}</div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <p className="font-medium text-muted-foreground mb-1">Remote IP</p>
                <p className="font-mono">{log.remoteIp}</p>
              </div>
              <div>
                <p className="font-medium text-muted-foreground mb-1">Created</p>
                <p>{formatDate(log.created)}</p>
              </div>
              {log.referer && (
                <div className="col-span-2">
                  <p className="font-medium text-muted-foreground mb-1">Referer</p>
                  <p className="font-mono text-xs break-all">{log.referer}</p>
                </div>
              )}
              {log.userAgent && (
                <div className="col-span-2">
                  <p className="font-medium text-muted-foreground mb-1">User Agent</p>
                  <p className="font-mono text-xs break-all">{log.userAgent}</p>
                </div>
              )}
            </div>
            {log.meta && Object.keys(log.meta).length > 0 && (
              <div>
                <p className="font-medium text-muted-foreground mb-2">Meta</p>
                <pre className="rounded-md bg-muted p-3 text-xs overflow-auto">
                  {JSON.stringify(log.meta, null, 2)}
                </pre>
              </div>
            )}
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">Log not found.</p>
        )}
      </SheetContent>
    </Sheet>
  )
}

interface LogsTableProps {
  logs: LogRecord[]
  isLoading?: boolean
}

export function LogsTable({ logs, isLoading }: LogsTableProps) {
  const [selectedId, setSelectedId] = useState<string | null>(null)

  if (isLoading) {
    return (
      <div className="flex justify-center py-16">
        <LoadingSpinner />
      </div>
    )
  }

  if (!logs.length) {
    return (
      <div className="flex flex-col items-center justify-center py-16 text-center">
        <Activity className="h-10 w-10 text-muted-foreground mb-3" />
        <p className="text-sm text-muted-foreground">No request logs found.</p>
      </div>
    )
  }

  return (
    <>
      <div className="rounded-md border bg-white overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b bg-slate-50 text-left">
              <th className="px-4 py-2.5 font-medium text-slate-600 w-20">Method</th>
              <th className="px-4 py-2.5 font-medium text-slate-600">URL</th>
              <th className="px-4 py-2.5 font-medium text-slate-600 w-20">Status</th>
              <th className="px-4 py-2.5 font-medium text-slate-600 w-24">Duration</th>
              <th className="px-4 py-2.5 font-medium text-slate-600 w-36">IP</th>
              <th className="px-4 py-2.5 font-medium text-slate-600 w-36">Created</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {logs.map((log) => (
              <tr
                key={log.id}
                className="cursor-pointer hover:bg-slate-50 transition-colors"
                onClick={() => setSelectedId(log.id)}
              >
                <td className="px-4 py-2.5">
                  <Badge
                    className={cn(
                      'font-mono text-xs border',
                      METHOD_COLORS[log.method] ?? 'bg-slate-100 text-slate-700 border-slate-200',
                    )}
                  >
                    {log.method}
                  </Badge>
                </td>
                <td className="px-4 py-2.5 font-mono text-xs text-slate-700 max-w-[300px] truncate">
                  {log.url}
                </td>
                <td className="px-4 py-2.5">
                  <Badge className={cn('font-mono text-xs border', statusColor(log.status))}>
                    {log.status}
                  </Badge>
                </td>
                <td className="px-4 py-2.5 text-slate-600">{log.execTime} ms</td>
                <td className="px-4 py-2.5 font-mono text-xs text-slate-600">{log.remoteIp}</td>
                <td className="px-4 py-2.5 text-slate-500 text-xs">{formatDate(log.created)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <LogDetailSheet logId={selectedId} onClose={() => setSelectedId(null)} />
    </>
  )
}
