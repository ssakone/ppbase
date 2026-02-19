import { useState } from 'react'
import { useLogs, useLogStats } from '@/hooks/use-logs'
import { ContentHeader } from '@/components/content-header'
import { Breadcrumb } from '@/components/breadcrumb'
import { LogsTable } from '@/components/logs-table'
import { LogsStatsCards } from '@/components/logs-stats-cards'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { RefreshCw, Search } from 'lucide-react'

const STATUS_OPTIONS = [
  { value: 'all', label: 'All statuses' },
  { value: '2xx', label: '2xx Success' },
  { value: '3xx', label: '3xx Redirect' },
  { value: '4xx', label: '4xx Client Error' },
  { value: '5xx', label: '5xx Server Error' },
]

const METHOD_OPTIONS = [
  { value: 'all', label: 'All methods' },
  { value: 'GET', label: 'GET' },
  { value: 'POST', label: 'POST' },
  { value: 'PATCH', label: 'PATCH' },
  { value: 'DELETE', label: 'DELETE' },
]

function buildFilter(search: string, status: string, method: string): string {
  const parts: string[] = []

  if (search) {
    parts.push(`url~"${search}"`)
  }

  if (status !== 'all') {
    const base = parseInt(status[0]) * 100
    parts.push(`status>=${base} && status<${base + 100}`)
  }

  if (method !== 'all') {
    parts.push(`method="${method}"`)
  }

  return parts.join(' && ')
}

export function LogsPage() {
  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState('all')
  const [method, setMethod] = useState('all')
  const [appliedSearch, setAppliedSearch] = useState('')
  const [appliedStatus, setAppliedStatus] = useState('all')
  const [appliedMethod, setAppliedMethod] = useState('all')

  const filter = buildFilter(appliedSearch, appliedStatus, appliedMethod)

  const { data, isLoading, refetch } = useLogs({ page, perPage: 30, filter })
  const { data: stats, isLoading: statsLoading, refetch: refetchStats } = useLogStats()

  const handleApplyFilter = () => {
    setAppliedSearch(search)
    setAppliedStatus(status)
    setAppliedMethod(method)
    setPage(1)
  }

  const handleRefresh = () => {
    refetch()
    refetchStats()
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') handleApplyFilter()
  }

  const logs = data?.items ?? []
  const totalPages = data?.totalPages ?? 1

  return (
    <>
      <ContentHeader
        left={<Breadcrumb items={[{ label: 'Logs', active: true }]} />}
        right={
          <Button variant="outline" size="sm" onClick={handleRefresh}>
            <RefreshCw className="h-4 w-4 mr-1.5" />
            Refresh
          </Button>
        }
      />

      <div className="flex-1 overflow-auto p-4 md:p-6 space-y-4">
        {/* Stats */}
        <LogsStatsCards stats={stats} isLoading={statsLoading} />

        {/* Filters */}
        <div className="flex flex-wrap gap-2 items-end">
          <div className="relative flex-1 min-w-[200px]">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input
              placeholder="Filter by URL..."
              className="pl-8"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              onKeyDown={handleKeyDown}
            />
          </div>
          <Select value={status} onValueChange={setStatus}>
            <SelectTrigger className="w-[160px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {STATUS_OPTIONS.map((o) => (
                <SelectItem key={o.value} value={o.value}>
                  {o.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select value={method} onValueChange={setMethod}>
            <SelectTrigger className="w-[140px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {METHOD_OPTIONS.map((o) => (
                <SelectItem key={o.value} value={o.value}>
                  {o.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button onClick={handleApplyFilter}>Apply</Button>
        </div>

        {/* Table */}
        <LogsTable logs={logs} isLoading={isLoading} />

        {/* Pagination */}
        {!isLoading && totalPages > 1 && (
          <div className="flex items-center justify-between text-sm text-muted-foreground">
            <span>
              Page {page} of {totalPages} — {data?.totalItems ?? 0} total requests
            </span>
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={page <= 1}
                onClick={() => setPage((p) => p - 1)}
              >
                Previous
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={page >= totalPages}
                onClick={() => setPage((p) => p + 1)}
              >
                Next
              </Button>
            </div>
          </div>
        )}
      </div>
    </>
  )
}
