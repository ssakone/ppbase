import type { LogStats } from '@/api/types'
import { Activity, AlertCircle, Clock, Globe } from 'lucide-react'

interface LogsStatsCardsProps {
  stats?: LogStats
  isLoading?: boolean
}

function StatCard({
  icon: Icon,
  label,
  value,
  className,
}: {
  icon: React.ElementType
  label: string
  value: string | number
  className?: string
}) {
  return (
    <div className="flex items-center gap-4 rounded-lg border bg-white p-4">
      <div className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-lg ${className}`}>
        <Icon className="h-5 w-5" />
      </div>
      <div>
        <p className="text-2xl font-semibold tabular-nums">{value}</p>
        <p className="text-sm text-muted-foreground">{label}</p>
      </div>
    </div>
  )
}

export function LogsStatsCards({ stats, isLoading }: LogsStatsCardsProps) {
  if (isLoading) {
    return (
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        {[...Array(4)].map((_, i) => (
          <div key={i} className="rounded-lg border bg-white p-4 h-[82px] animate-pulse bg-slate-50" />
        ))}
      </div>
    )
  }

  return (
    <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
      <StatCard
        icon={Activity}
        label="Total requests"
        value={stats?.total ?? 0}
        className="bg-indigo-50 text-indigo-600"
      />
      <StatCard
        icon={AlertCircle}
        label="Errors (4xx/5xx)"
        value={stats?.errors ?? 0}
        className="bg-red-50 text-red-600"
      />
      <StatCard
        icon={Clock}
        label="Avg response (ms)"
        value={stats ? Math.round(stats.avgMs) : 0}
        className="bg-amber-50 text-amber-600"
      />
      <StatCard
        icon={Globe}
        label="Unique IPs"
        value={stats?.uniqueIps ?? 0}
        className="bg-green-50 text-green-600"
      />
    </div>
  )
}
