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
import { Activity, Clock, Globe, Link2, Monitor, Tag } from 'lucide-react'

const METHOD_COLORS: Record<string, string> = {
  GET: 'bg-blue-100 text-blue-700 border-blue-200',
  POST: 'bg-green-100 text-green-700 border-green-200',
  PATCH: 'bg-amber-100 text-amber-700 border-amber-200',
  PUT: 'bg-amber-100 text-amber-700 border-amber-200',
  DELETE: 'bg-red-100 text-red-700 border-red-200',
}

const METHOD_BG: Record<string, string> = {
  GET: 'bg-blue-50 border-blue-100',
  POST: 'bg-green-50 border-green-100',
  PATCH: 'bg-amber-50 border-amber-100',
  PUT: 'bg-amber-50 border-amber-100',
  DELETE: 'bg-red-50 border-red-100',
}

function statusColor(status: number): string {
  if (status >= 500) return 'bg-red-100 text-red-700 border-red-200'
  if (status >= 400) return 'bg-amber-100 text-amber-700 border-amber-200'
  if (status >= 300) return 'bg-blue-100 text-blue-700 border-blue-200'
  return 'bg-green-100 text-green-700 border-green-200'
}

function statusLabel(status: number): string {
  const map: Record<number, string> = {
    200: 'OK', 201: 'Created', 204: 'No Content',
    301: 'Moved Permanently', 302: 'Found', 304: 'Not Modified',
    400: 'Bad Request', 401: 'Unauthorized', 403: 'Forbidden',
    404: 'Not Found', 409: 'Conflict', 422: 'Unprocessable',
    500: 'Internal Error', 502: 'Bad Gateway', 503: 'Unavailable',
  }
  return map[status] ?? ''
}

function formatDate(iso: string) {
  return new Date(iso).toLocaleString(undefined, {
    year: 'numeric',
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

function DetailRow({ icon, label, value, mono = false }: {
  icon: React.ReactNode
  label: string
  value: React.ReactNode
  mono?: boolean
}) {
  return (
    <div className="flex gap-3 py-3 border-b border-slate-100 last:border-0">
      <div className="mt-0.5 text-slate-400 shrink-0">{icon}</div>
      <div className="min-w-0 flex-1">
        <p className="text-[11px] font-semibold uppercase tracking-widest text-slate-400 mb-0.5">{label}</p>
        <div className={cn('text-[14px] text-slate-800 break-all', mono && 'font-mono text-[13px]')}>
          {value}
        </div>
      </div>
    </div>
  )
}

function LogDetailSheet({ logId, onClose }: LogDetailSheetProps) {
  const { data: log, isLoading } = useLog(logId ?? undefined)

  return (
    <Sheet open={!!logId} onOpenChange={(o) => !o && onClose()}>
      <SheetContent className="sm:max-w-[560px] flex flex-col p-0 gap-0 overflow-hidden">
        {/* Header */}
        <SheetHeader className="px-6 pt-6 pb-4 border-b border-slate-100 shrink-0">
          <SheetTitle className="text-[17px] font-semibold text-slate-900">
            Request Details
          </SheetTitle>
        </SheetHeader>

        {isLoading ? (
          <div className="flex flex-1 items-center justify-center py-20">
            <LoadingSpinner />
          </div>
        ) : log ? (
          <div className="flex-1 overflow-y-auto">
            {/* Hero summary card */}
            <div className={cn(
              'mx-6 mt-5 rounded-xl border p-4',
              METHOD_BG[log.method] ?? 'bg-slate-50 border-slate-100',
            )}>
              <div className="flex flex-wrap items-center gap-2 mb-3">
                <Badge className={cn('font-mono text-[12px] font-bold px-2.5 py-0.5 border', METHOD_COLORS[log.method] ?? 'bg-slate-100 text-slate-700 border-slate-200')}>
                  {log.method}
                </Badge>
                <Badge className={cn('font-mono text-[12px] font-bold px-2.5 py-0.5 border', statusColor(log.status))}>
                  {log.status}{statusLabel(log.status) ? ` ${statusLabel(log.status)}` : ''}
                </Badge>
                <span className="ml-auto text-[13px] font-semibold text-slate-600 tabular-nums">
                  {log.execTime} ms
                </span>
              </div>
              <p className="font-mono text-[12px] text-slate-700 break-all leading-relaxed">
                {log.url}
              </p>
            </div>

            {/* Detail rows */}
            <div className="px-6 pt-4 pb-6">
              <DetailRow
                icon={<Clock className="h-4 w-4" />}
                label="Timestamp"
                value={formatDate(log.created)}
              />
              {log.remoteIp && (
                <DetailRow
                  icon={<Globe className="h-4 w-4" />}
                  label="Remote IP"
                  value={log.remoteIp}
                  mono
                />
              )}
              {log.referer && (
                <DetailRow
                  icon={<Link2 className="h-4 w-4" />}
                  label="Referer"
                  value={log.referer}
                  mono
                />
              )}
              {log.userAgent && (
                <DetailRow
                  icon={<Monitor className="h-4 w-4" />}
                  label="User Agent"
                  value={log.userAgent}
                  mono
                />
              )}
              <DetailRow
                icon={<Tag className="h-4 w-4" />}
                label="Log ID"
                value={log.id}
                mono
              />

              {/* Meta section */}
              {log.meta && Object.keys(log.meta).length > 0 && (
                <div className="mt-5">
                  <p className="text-[11px] font-semibold uppercase tracking-widest text-slate-400 mb-2">
                    Meta
                  </p>
                  <pre className="rounded-xl bg-slate-950 text-green-400 p-4 text-[12px] font-mono overflow-auto leading-relaxed">
                    {JSON.stringify(log.meta, null, 2)}
                  </pre>
                </div>
              )}
            </div>
          </div>
        ) : (
          <div className="flex flex-col items-center justify-center flex-1 py-20 text-center px-6">
            <Activity className="h-10 w-10 text-slate-300 mb-3" />
            <p className="text-[14px] text-slate-500">Log entry not found.</p>
          </div>
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
