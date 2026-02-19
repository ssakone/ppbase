import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  BookOpen,
  CheckCircle2,
  Database,
  GitBranch,
  Home,
  Plus,
  Settings,
  ShieldCheck,
  Table2,
} from 'lucide-react'
import { ContentHeader } from '@/components/content-header'
import { Breadcrumb } from '@/components/breadcrumb'
import { Button } from '@/components/ui/button'
import { useCollections } from '@/hooks/use-collections'
import { useLogs, useLogStats } from '@/hooks/use-logs'
import { useMigrations, useMigrationStatus } from '@/hooks/use-migrations'
import { useSettings } from '@/hooks/use-settings'
import { useHealth } from '@/hooks/use-health'
import { formatDate } from '@/lib/utils'
import { prefetchRoute } from '@/lib/route-prefetch'

function StatCard({
  label,
  value,
  detail,
  icon: Icon,
  tone,
}: {
  label: string
  value: string | number
  detail: string
  icon: React.ElementType
  tone: 'indigo' | 'green' | 'amber' | 'slate'
}) {
  const toneClass = {
    indigo: 'bg-indigo-50 text-indigo-600 border-indigo-100',
    green: 'bg-emerald-50 text-emerald-600 border-emerald-100',
    amber: 'bg-amber-50 text-amber-600 border-amber-100',
    slate: 'bg-slate-100 text-slate-600 border-slate-200',
  }[tone]

  return (
    <div className="rounded-xl border bg-white p-4 shadow-sm card-lift">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            {label}
          </p>
          <p className="mt-2 text-3xl font-semibold tracking-tight tabular-nums">
            {value}
          </p>
          <p className="mt-1 text-sm text-muted-foreground">{detail}</p>
        </div>
        <span className={`inline-flex h-10 w-10 items-center justify-center rounded-lg border ${toneClass}`}>
          <Icon className="h-5 w-5" />
        </span>
      </div>
    </div>
  )
}

export function DashboardPage() {
  const [isRefreshing, setIsRefreshing] = useState(false)
  const [lastRefreshAt, setLastRefreshAt] = useState<Date | null>(null)

  const {
    data: collections = [],
    isLoading: collectionsLoading,
    refetch: refetchCollections,
  } = useCollections()
  const {
    data: logStats,
    isLoading: logsLoading,
    refetch: refetchLogStats,
  } = useLogStats()
  const {
    data: recentErrorsData,
    isLoading: recentErrorsLoading,
    refetch: refetchRecentErrors,
  } = useLogs({
    page: 1,
    perPage: 5,
    sort: '-created',
    filter: 'status>=400',
  })
  const {
    data: migrationStatus,
    isLoading: migrationsLoading,
    refetch: refetchMigrationStatus,
  } = useMigrationStatus()
  const {
    data: migrationsData,
    refetch: refetchMigrations,
  } = useMigrations()
  const {
    data: settings,
    isLoading: settingsLoading,
    refetch: refetchSettings,
  } = useSettings()
  const {
    data: health,
    isLoading: healthLoading,
    isError: healthError,
    refetch: refetchHealth,
  } = useHealth()

  const totalCollections = collections.length
  const authCollections = collections.filter((item) => item.type === 'auth').length
  const viewCollections = collections.filter((item) => item.type === 'view').length
  const baseCollections = collections.filter((item) => item.type === 'base').length

  const recentCollections = [...collections]
    .sort((a, b) => new Date(b.updated).getTime() - new Date(a.updated).getTime())
    .slice(0, 6)

  const recentErrors = recentErrorsData?.items ?? []
  const appName = String(settings?.meta?.appName || 'PPBase')
  const migrationFilesCount = migrationsData?.totalItems ?? 0
  const errorRatePercent = logStats?.total ? (logStats.errors / logStats.total) * 100 : 0
  const highErrorRate = (logStats?.total ?? 0) >= 20 && errorRatePercent >= 5
  const hasPendingMigrations = (migrationStatus?.pending ?? 0) > 0
  const healthy = !healthError && health?.code === 200

  const alerts = useMemo(() => {
    const values: Array<{ title: string; detail: string; href: string; tone: 'warn' | 'danger' }> = []
    if (!healthy) {
      values.push({
        title: 'API health check failed',
        detail: 'Dashboard cannot confirm backend readiness.',
        href: '/logs',
        tone: 'danger',
      })
    }
    if (hasPendingMigrations) {
      values.push({
        title: `${migrationStatus?.pending ?? 0} migration(s) pending`,
        detail: 'Apply pending migrations to keep schema and code in sync.',
        href: '/migrations',
        tone: 'warn',
      })
    }
    if (highErrorRate) {
      values.push({
        title: `High error rate (${errorRatePercent.toFixed(1)}%)`,
        detail: 'Review failing requests and inspect logs.',
        href: '/logs',
        tone: 'danger',
      })
    }
    if (authCollections === 0) {
      values.push({
        title: 'No auth collection configured',
        detail: 'Create at least one auth collection for user sign-in flows.',
        href: '/collections?new=1',
        tone: 'warn',
      })
    }
    return values
  }, [healthy, hasPendingMigrations, migrationStatus?.pending, highErrorRate, errorRatePercent, authCollections])

  const handleRefresh = async () => {
    setIsRefreshing(true)
    try {
      await Promise.all([
        refetchCollections(),
        refetchLogStats(),
        refetchRecentErrors(),
        refetchMigrationStatus(),
        refetchMigrations(),
        refetchSettings(),
        refetchHealth(),
      ])
      setLastRefreshAt(new Date())
    } finally {
      setIsRefreshing(false)
    }
  }

  return (
    <>
      <ContentHeader
        left={<Breadcrumb items={[{ label: 'Dashboard', active: true }]} />}
        right={
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={handleRefresh} disabled={isRefreshing}>
              <Activity className={`h-4 w-4 ${isRefreshing ? 'animate-spin' : ''}`} />
              {isRefreshing ? 'Refreshing...' : 'Refresh overview'}
            </Button>
            <Button asChild size="sm">
              <Link
                to="/collections?new=1"
                onMouseEnter={() => prefetchRoute('collections')}
                onFocus={() => prefetchRoute('collections')}
              >
                <Plus className="h-4 w-4" />
                New collection
              </Link>
            </Button>
          </div>
        }
      />

      <div className="flex-1 overflow-auto p-4 md:p-6">
        <div className="mx-auto max-w-7xl space-y-6">
          <section className="rounded-2xl border bg-gradient-to-r from-indigo-600 to-indigo-500 p-6 text-white shadow-md animate-enter-up">
            <div className="flex flex-wrap items-center justify-between gap-4">
              <div>
                <p className="text-sm font-medium text-indigo-100">Admin Overview</p>
                <h1 className="mt-1 text-2xl font-semibold tracking-tight">{appName}</h1>
                <p className="mt-2 text-sm text-indigo-100">
                  Track collections, health, logs and migrations in one place.
                </p>
              </div>
              <div className="rounded-xl bg-white/10 px-4 py-3 text-sm backdrop-blur">
                <p className="text-indigo-100">System status</p>
                <div className="mt-1 flex items-center gap-2">
                  {healthLoading ? (
                    <span className="text-white">Checking...</span>
                  ) : healthy ? (
                    <>
                      <CheckCircle2 className="h-4 w-4 text-emerald-200" />
                      <span className="font-semibold text-white">Healthy</span>
                    </>
                  ) : (
                    <>
                      <AlertTriangle className="h-4 w-4 text-amber-200" />
                      <span className="font-semibold text-white">Attention required</span>
                    </>
                  )}
                </div>
                <p className="mt-1 text-xs text-indigo-100">
                  {lastRefreshAt ? `Updated ${formatDate(lastRefreshAt.toISOString())}` : 'Use refresh to reload data'}
                </p>
              </div>
            </div>
          </section>

          {alerts.length > 0 && (
            <section className="rounded-xl border border-amber-200 bg-amber-50 p-4 animate-enter-up animate-enter-delay-1">
              <h2 className="mb-3 text-sm font-semibold text-amber-900">Attention required</h2>
              <div className="space-y-2">
                {alerts.map((alert, index) => (
                  <Link
                    key={`${alert.title}-${index}`}
                    to={alert.href}
                    className={`block rounded-lg border bg-white px-3 py-2.5 hover:bg-slate-50 ${
                      alert.tone === 'danger' ? 'border-red-200' : 'border-amber-200'
                    }`}
                    onMouseEnter={() => {
                      if (alert.href.startsWith('/logs')) prefetchRoute('logs')
                      if (alert.href.startsWith('/migrations')) prefetchRoute('migrations')
                      if (alert.href.startsWith('/collections')) prefetchRoute('collections')
                    }}
                    onFocus={() => {
                      if (alert.href.startsWith('/logs')) prefetchRoute('logs')
                      if (alert.href.startsWith('/migrations')) prefetchRoute('migrations')
                      if (alert.href.startsWith('/collections')) prefetchRoute('collections')
                    }}
                  >
                    <p className="text-sm font-semibold text-slate-900">{alert.title}</p>
                    <p className="text-xs text-slate-600">{alert.detail}</p>
                  </Link>
                ))}
              </div>
            </section>
          )}

          <section className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4 animate-enter-up animate-enter-delay-1">
            <StatCard
              label="Collections"
              value={collectionsLoading ? '...' : totalCollections}
              detail={`${baseCollections} base, ${authCollections} auth`}
              icon={Database}
              tone="indigo"
            />
            <StatCard
              label="View Collections"
              value={collectionsLoading ? '...' : viewCollections}
              detail="SQL view-backed collections"
              icon={Table2}
              tone="slate"
            />
            <StatCard
              label="Requests"
              value={logsLoading ? '...' : logStats?.total ?? 0}
              detail={`error rate ${errorRatePercent.toFixed(1)}%`}
              icon={BookOpen}
              tone="green"
            />
            <StatCard
              label="Migrations Pending"
              value={migrationsLoading ? '...' : migrationStatus?.pending ?? 0}
              detail={`Applied ${migrationStatus?.applied ?? 0} / ${migrationFilesCount}`}
              icon={GitBranch}
              tone="amber"
            />
          </section>

          <section className="grid grid-cols-1 gap-4 lg:grid-cols-3 animate-enter-up animate-enter-delay-2">
            <div className="rounded-xl border bg-white p-5 shadow-sm lg:col-span-1 card-lift">
              <div className="mb-4 flex items-center justify-between">
                <h2 className="text-base font-semibold">Recent collections</h2>
                <Button asChild variant="ghost" size="sm">
                  <Link
                    to="/collections"
                    onMouseEnter={() => prefetchRoute('collections')}
                    onFocus={() => prefetchRoute('collections')}
                  >
                    Open
                    <ArrowRight className="h-4 w-4" />
                  </Link>
                </Button>
              </div>

              {collectionsLoading ? (
                <div className="space-y-2">
                  {[1, 2, 3].map((idx) => (
                    <div key={idx} className="h-14 rounded-lg skeleton-shimmer" />
                  ))}
                </div>
              ) : recentCollections.length === 0 ? (
                <p className="text-sm text-muted-foreground">No collections yet.</p>
              ) : (
                <div className="space-y-2">
                  {recentCollections.map((collection) => (
                    <Link
                      key={collection.id}
                      to={`/collections/${collection.id}`}
                      className="flex items-center justify-between rounded-lg border border-slate-200 px-3 py-2.5 text-sm hover:bg-slate-50 transition-all duration-200 hover:shadow-sm"
                      onMouseEnter={() => prefetchRoute('records')}
                      onFocus={() => prefetchRoute('records')}
                    >
                      <div>
                        <p className="font-medium text-foreground">{collection.name}</p>
                        <p className="text-xs text-muted-foreground">
                          {collection.type} · {formatDate(collection.updated)}
                        </p>
                      </div>
                      <ArrowRight className="h-4 w-4 text-muted-foreground" />
                    </Link>
                  ))}
                </div>
              )}
            </div>

            <div className="rounded-xl border bg-white p-5 shadow-sm lg:col-span-1 card-lift">
              <div className="mb-4 flex items-center justify-between">
                <h2 className="text-base font-semibold">Recent errors</h2>
                <Button asChild variant="ghost" size="sm">
                  <Link to="/logs" onMouseEnter={() => prefetchRoute('logs')} onFocus={() => prefetchRoute('logs')}>
                    Open
                    <ArrowRight className="h-4 w-4" />
                  </Link>
                </Button>
              </div>
              {recentErrorsLoading ? (
                <div className="space-y-2">
                  {[1, 2, 3].map((idx) => (
                    <div key={idx} className="h-14 rounded-lg skeleton-shimmer" />
                  ))}
                </div>
              ) : recentErrors.length === 0 ? (
                <p className="text-sm text-muted-foreground">No error logs recorded.</p>
              ) : (
                <div className="space-y-2">
                  {recentErrors.map((item) => (
                    <div key={item.id} className="rounded-lg border border-red-100 bg-red-50/40 px-3 py-2.5 transition-all duration-200 hover:shadow-sm">
                      <div className="flex items-center justify-between gap-2">
                        <p className="truncate text-sm font-medium text-slate-900">{item.method} {item.url}</p>
                        <span className="rounded-md bg-red-100 px-2 py-0.5 text-xs font-semibold text-red-700">
                          {item.status}
                        </span>
                      </div>
                      <p className="mt-1 text-xs text-slate-600">{formatDate(item.created)}</p>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div className="rounded-xl border bg-white p-5 shadow-sm lg:col-span-1 card-lift">
              <h2 className="mb-4 text-base font-semibold">Quick actions</h2>
              <div className="grid grid-cols-1 gap-2.5">
                <Button asChild variant="outline" className="justify-start">
                  <Link
                    to="/collections?new=1"
                    onMouseEnter={() => prefetchRoute('collections')}
                    onFocus={() => prefetchRoute('collections')}
                  >
                    <Plus className="h-4 w-4" />
                    Create collection
                  </Link>
                </Button>
                <Button asChild variant="outline" className="justify-start">
                  <Link
                    to="/migrations"
                    onMouseEnter={() => prefetchRoute('migrations')}
                    onFocus={() => prefetchRoute('migrations')}
                  >
                    <GitBranch className="h-4 w-4" />
                    Review migrations
                  </Link>
                </Button>
                <Button asChild variant="outline" className="justify-start">
                  <Link to="/logs" onMouseEnter={() => prefetchRoute('logs')} onFocus={() => prefetchRoute('logs')}>
                    <Activity className="h-4 w-4" />
                    Inspect logs
                  </Link>
                </Button>
                <Button asChild variant="outline" className="justify-start">
                  <Link
                    to="/settings"
                    onMouseEnter={() => prefetchRoute('settings')}
                    onFocus={() => prefetchRoute('settings')}
                  >
                    <Settings className="h-4 w-4" />
                    Update settings
                  </Link>
                </Button>
              </div>

              <div className="mt-4 rounded-lg border border-slate-200 bg-slate-50 p-3">
                <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  Access control
                </p>
                <p className="mt-1 text-sm text-slate-700">
                  Superuser management collection:
                  {' '}
                  <Link
                    to="/collections/_superusers"
                    className="font-medium text-indigo-600 hover:underline"
                    onMouseEnter={() => prefetchRoute('records')}
                    onFocus={() => prefetchRoute('records')}
                  >
                    _superusers
                  </Link>
                </p>
                <div className="mt-2 flex items-center gap-1.5 text-xs text-slate-500">
                  <ShieldCheck className="h-3.5 w-3.5" />
                  Admin-only area
                </div>
                <Button asChild variant="ghost" size="sm" className="mt-2 justify-start px-0">
                  <a href="/api/docs" target="_blank" rel="noreferrer">
                    <Home className="h-4 w-4" />
                    Open API docs
                  </a>
                </Button>
              </div>
            </div>
          </section>
        </div>
      </div>
    </>
  )
}
