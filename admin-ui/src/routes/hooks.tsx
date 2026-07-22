import { useEffect, useState } from 'react'
import {
  useHooks,
  useUpdateHook,
  useRescanHooks,
  useReloadHook,
  useUpdateHooksRuntime,
  useRestartPPBase,
} from '@/hooks/use-hooks'
import { ContentHeader } from '@/components/content-header'
import { Breadcrumb } from '@/components/breadcrumb'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Checkbox } from '@/components/ui/checkbox'
import { LoadingSpinner } from '@/components/loading-spinner'
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip'
import {
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  FolderSearch,
  Pause,
  Play,
  RefreshCw,
  RotateCw,
  Power,
  Zap,
} from 'lucide-react'
import { toast } from 'sonner'
import type { HookState } from '@/api/types'

type ActionState = 'restart' | 'rescan' | 'refresh' | null

function StatusBadge({ status }: { status: HookState['status'] }) {
  switch (status) {
    case 'loaded':
      return (
        <Badge className="bg-emerald-100 text-emerald-700 border-emerald-200 hover:bg-emerald-100">
          <CheckCircle2 className="h-3 w-3 mr-1" />
          Loaded
        </Badge>
      )
    case 'error':
      return (
        <Badge className="bg-red-100 text-red-700 border-red-200 hover:bg-red-100">
          <AlertCircle className="h-3 w-3 mr-1" />
          Error
        </Badge>
      )
    case 'disabled':
      return (
        <Badge className="bg-slate-100 text-slate-500 border-slate-200 hover:bg-slate-100">
          <Pause className="h-3 w-3 mr-1" />
          Disabled
        </Badge>
      )
    case 'unsupported_for_hot_reload':
      return (
        <Badge className="bg-amber-100 text-amber-700 border-amber-200 hover:bg-amber-100">
          <AlertTriangle className="h-3 w-3 mr-1" />
          Restart required
        </Badge>
      )
    default:
      return <Badge variant="outline">{status}</Badge>
  }
}

function HookCard({
  hook,
  onToggle,
  onReload,
  toggling,
  reloading,
}: {
  hook: HookState
  onToggle: (hookId: string, enabled: boolean) => void
  onReload: (hookId: string) => void
  toggling: boolean
  reloading: boolean
}) {
  const [expanded, setExpanded] = useState(false)

  return (
    <div className="rounded-lg border bg-white shadow-sm">
      <div className="flex items-start gap-4 px-5 py-4">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2.5 mb-1">
            <h3 className="text-sm font-semibold text-slate-900 truncate">
              {hook.name}
            </h3>
            <StatusBadge status={hook.status} />
            {hook.hasRoutes && (
              <Badge variant="outline" className="text-[10px] text-amber-600 border-amber-200 bg-amber-50">
                Has routes
              </Badge>
            )}
            {hook.hasHttpMiddleware && (
              <Badge variant="outline" className="text-[10px] text-amber-600 border-amber-200 bg-amber-50">
                Has HTTP middleware
              </Badge>
            )}
          </div>
          <p className="text-xs text-slate-500 mb-1 font-mono truncate">
            {hook.filename}
          </p>
          {hook.description && (
            <p className="text-xs text-slate-600 mt-1">{hook.description}</p>
          )}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <TooltipProvider delayDuration={200}>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8"
                  onClick={() => onReload(hook.id)}
                  disabled={reloading || !hook.enabled}
                >
                  {reloading ? (
                    <LoadingSpinner size="sm" />
                  ) : (
                    <RotateCw className="h-3.5 w-3.5" />
                  )}
                </Button>
              </TooltipTrigger>
              <TooltipContent side="top" className="text-xs">Reload</TooltipContent>
            </Tooltip>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant={hook.enabled ? 'ghost' : 'ghost'}
                  size="icon"
                  className={`h-8 w-8 ${hook.enabled ? 'text-slate-500 hover:text-red-500' : 'text-emerald-600 hover:text-emerald-700'}`}
                  onClick={() => onToggle(hook.id, !hook.enabled)}
                  disabled={toggling}
                >
                  {toggling ? (
                    <LoadingSpinner size="sm" />
                  ) : hook.enabled ? (
                    <Pause className="h-3.5 w-3.5" />
                  ) : (
                    <Play className="h-3.5 w-3.5" />
                  )}
                </Button>
              </TooltipTrigger>
              <TooltipContent side="top" className="text-xs">
                {hook.enabled ? 'Disable' : 'Enable'}
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        </div>
      </div>

      {(hook.functions.length > 0 || hook.error || hook.restartRequired) && (
        <div className="border-t">
          <button
            className="flex items-center gap-1.5 w-full px-5 py-2 text-xs text-slate-500 hover:bg-slate-50 transition-colors"
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? (
              <ChevronDown className="h-3 w-3" />
            ) : (
              <ChevronRight className="h-3 w-3" />
            )}
            {hook.error
              ? 'Error details'
              : hook.restartRequired
                ? 'Restart details'
              : `${hook.functions.length} registered hook${hook.functions.length !== 1 ? 's' : ''}`}
          </button>
          {expanded && (
            <div className="px-5 pb-3 space-y-2">
              {hook.restartRequired && !hook.error && (
                <div className="rounded-md bg-amber-50 border border-amber-200 p-3">
                  <p className="text-xs font-medium text-amber-800">
                    Route or middleware changes were detected. Hook handlers were refreshed when possible, but route and middleware updates need a PPBase restart to apply.
                  </p>
                </div>
              )}
              {hook.error && (
                <div className={`rounded-md border p-3 ${hook.status === 'unsupported_for_hot_reload' ? 'bg-amber-50 border-amber-200' : 'bg-red-50 border-red-200'}`}>
                  <p className={`text-xs font-medium mb-1 ${hook.status === 'unsupported_for_hot_reload' ? 'text-amber-800' : 'text-red-800'}`}>
                    {hook.error}
                  </p>
                  {hook.errorTraceback && (
                    <pre className="text-[10px] text-red-700 whitespace-pre-wrap overflow-x-auto max-h-48 mt-2 font-mono leading-relaxed">
                      {hook.errorTraceback}
                    </pre>
                  )}
                </div>
              )}
              {hook.functions.length > 0 && (
                <div className="space-y-1">
                  {hook.functions.map((fn, i) => (
                    <div
                      key={i}
                      className="flex items-center gap-2 text-xs text-slate-600"
                    >
                      <Zap className="h-3 w-3 text-indigo-400 shrink-0" />
                      <span className="font-mono text-slate-700">
                        {fn.handlerName}
                      </span>
                      <span className="text-slate-400">on</span>
                      <Badge
                        variant="outline"
                        className="text-[10px] px-1.5 py-0"
                      >
                        {fn.hookType}
                      </Badge>
                    </div>
                  ))}
                </div>
              )}
              {hook.lastLoaded && (
                <p className="text-[10px] text-slate-400 pt-1">
                  Last loaded: {new Date(hook.lastLoaded).toLocaleString()}
                </p>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export function HooksPage() {
  const { data, isLoading, isError, isFetching, refetch } = useHooks()
  const updateHook = useUpdateHook()
  const rescanMutation = useRescanHooks()
  const reloadMutation = useReloadHook()
  const updateRuntime = useUpdateHooksRuntime()
  const restartMutation = useRestartPPBase()

  const [togglingId, setTogglingId] = useState<string | null>(null)
  const [reloadingId, setReloadingId] = useState<string | null>(null)
  const [activeAction, setActiveAction] = useState<ActionState>(null)

  const hooks = data?.items ?? []
  const hooksDir = data?.hooksDir ?? './pb_hooks'
  const runtime = data?.runtime ?? {
    disabled: [],
    autoRestartOnChange: false,
    canRestart: false,
    restartPending: false,
  }

  useEffect(() => {
    if (!runtime.restartPending) {
      return
    }

    const interval = window.setInterval(() => {
      void refetch()
    }, 1500)

    return () => {
      window.clearInterval(interval)
    }
  }, [runtime.restartPending, refetch])

  const handleToggle = async (hookId: string, enabled: boolean) => {
    setTogglingId(hookId)
    try {
      await updateHook.mutateAsync({ hookId, enabled })
      toast.success(enabled ? 'Hook enabled' : 'Hook disabled')
    } catch {
      toast.error('Failed to update hook')
    } finally {
      setTogglingId(null)
    }
  }

  const handleReload = async (hookId: string) => {
    setReloadingId(hookId)
    try {
      await reloadMutation.mutateAsync(hookId)
      toast.success('Hook reloaded')
    } catch {
      toast.error('Failed to reload hook')
    } finally {
      setReloadingId(null)
    }
  }

  const handleRescan = async () => {
    setActiveAction('rescan')
    try {
      const result = await rescanMutation.mutateAsync()
      await refetch()
      if (result.reloaded.length > 0) {
        toast.success(
          runtime.autoRestartOnChange
            ? `Detected ${result.reloaded.length} changed file(s). Restart scheduled.`
            : `Rescanned: ${result.reloaded.length} file(s) updated`,
        )
      } else {
        toast.success('No changes detected')
      }
    } catch {
      toast.error('Failed to rescan hooks directory')
    } finally {
      setActiveAction(null)
    }
  }

  const handleToggleAutoRestart = async (checked: boolean) => {
    try {
      await updateRuntime.mutateAsync({ autoRestartOnChange: checked })
      toast.success(
        checked
          ? 'Auto restart on hook change enabled'
          : 'Auto restart on hook change disabled',
      )
    } catch {
      toast.error('Failed to update auto-restart setting')
    }
  }

  const handleRestart = async () => {
    setActiveAction('restart')
    try {
      await restartMutation.mutateAsync()
      await refetch()
      toast.success('PPBase restart scheduled. The app will restart shortly.')
    } catch {
      toast.error('Failed to schedule PPBase restart')
    } finally {
      setActiveAction(null)
    }
  }

  const handleRefresh = async () => {
    setActiveAction('refresh')
    try {
      await refetch()
    } finally {
      setActiveAction(null)
    }
  }

  const loadedCount = hooks.filter((h) => h.status === 'loaded').length
  const errorCount = hooks.filter((h) => h.status === 'error').length
  const disabledCount = hooks.filter((h) => h.status === 'disabled').length

  return (
    <>
      <ContentHeader
        left={<Breadcrumb items={[{ label: 'Hooks', active: true }]} />}
        right={
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={handleRestart}
              disabled={!runtime.canRestart || runtime.restartPending || restartMutation.isPending}
              className="min-w-[148px] justify-center"
            >
              {restartMutation.isPending || runtime.restartPending ? (
                <LoadingSpinner size="sm" className="mr-1.5" />
              ) : (
                <Power className="h-4 w-4 mr-1.5" />
              )}
              {restartMutation.isPending
                ? 'Scheduling...'
                : runtime.restartPending
                  ? 'Restart pending'
                  : 'Restart app'}
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={handleRescan}
              disabled={rescanMutation.isPending}
              className="min-w-[132px] justify-center"
            >
              {rescanMutation.isPending ? (
                <LoadingSpinner size="sm" className="mr-1.5" />
              ) : (
                <FolderSearch className="h-4 w-4 mr-1.5" />
              )}
              {rescanMutation.isPending ? 'Scanning...' : 'Rescan'}
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={handleRefresh}
              disabled={activeAction === 'refresh' || isFetching}
              className="min-w-[124px] justify-center"
            >
              {activeAction === 'refresh' || isFetching ? (
                <LoadingSpinner size="sm" className="mr-1.5" />
              ) : (
                <RefreshCw className="h-4 w-4 mr-1.5" />
              )}
              {activeAction === 'refresh' || isFetching ? 'Refreshing...' : 'Refresh'}
            </Button>
          </div>
        }
      />

      <div className="flex-1 overflow-auto p-4 md:p-6 space-y-4">
        {isLoading ? (
          <LoadingSpinner fullPage />
        ) : isError ? (
          <div className="flex flex-col items-center justify-center py-16 text-center">
            <AlertCircle className="h-10 w-10 text-destructive mb-3" />
            <h3 className="text-lg font-semibold mb-1">Failed to load hooks</h3>
            <p className="text-sm text-muted-foreground mb-4">
              Could not connect to the hooks API.
            </p>
            <Button variant="outline" onClick={() => refetch()}>
              Retry
            </Button>
          </div>
        ) : (
          <>
            {(activeAction !== null || runtime.restartPending || isFetching) && (
              <div className="rounded-lg border bg-slate-50 px-4 py-3">
                <div className="flex items-start gap-3">
                  <LoadingSpinner size="sm" className="mt-0.5 shrink-0" />
                  <div className="space-y-1">
                    <p className="text-sm font-medium text-slate-900">
                      {runtime.restartPending
                        ? 'Restart scheduled'
                        : activeAction === 'restart'
                          ? 'Scheduling app restart'
                          : activeAction === 'rescan'
                            ? 'Scanning hooks directory'
                            : activeAction === 'refresh'
                              ? 'Refreshing hook state'
                              : 'Updating hook status'}
                    </p>
                    <p className="text-xs text-slate-500">
                      {runtime.restartPending
                        ? 'PPBase accepted the restart request. This page will refresh automatically while waiting for the process state to change.'
                        : activeAction === 'restart'
                          ? 'Sending the restart request to PPBase.'
                          : activeAction === 'rescan'
                            ? 'Checking the hooks directory and reloading updated hook files.'
                            : activeAction === 'refresh'
                              ? 'Fetching the latest hook and runtime state.'
                              : 'Synchronizing the latest runtime information.'}
                    </p>
                  </div>
                </div>
              </div>
            )}

            <div className="rounded-lg border bg-white px-4 py-3 flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
              <div className="space-y-1">
                <p className="text-sm font-medium text-slate-900">Runtime behavior</p>
                <p className="text-xs text-slate-500">
                  Enable automatic PPBase restart when files inside <code className="bg-slate-100 px-1 py-0.5 rounded font-mono">{hooksDir}</code> change.
                </p>
              </div>
              <div className="flex items-center gap-3">
                <label className="flex items-center gap-2 text-sm text-slate-700">
                  <Checkbox
                    checked={runtime.autoRestartOnChange}
                    onCheckedChange={(value) => handleToggleAutoRestart(Boolean(value))}
                    disabled={updateRuntime.isPending}
                  />
                  Auto restart on hook change
                </label>
                {!runtime.canRestart && (
                  <Badge variant="outline" className="text-[10px]">
                    Restart unavailable
                  </Badge>
                )}
                {runtime.restartPending && (
                  <Badge className="bg-amber-100 text-amber-700 border-amber-200 hover:bg-amber-100">
                    Restart pending
                  </Badge>
                )}
              </div>
            </div>

            {/* Stats bar */}
            <div className="flex flex-wrap gap-3 items-center text-sm">
              <span className="text-slate-600">
                <span className="font-medium text-slate-900">{hooks.length}</span>{' '}
                hook file{hooks.length !== 1 ? 's' : ''} in{' '}
                <code className="text-xs bg-slate-100 px-1.5 py-0.5 rounded font-mono">
                  {hooksDir}
                </code>
              </span>
              {loadedCount > 0 && (
                <Badge className="bg-emerald-50 text-emerald-700 border-emerald-200 hover:bg-emerald-50">
                  {loadedCount} loaded
                </Badge>
              )}
              {errorCount > 0 && (
                <Badge className="bg-red-50 text-red-700 border-red-200 hover:bg-red-50">
                  {errorCount} error{errorCount !== 1 ? 's' : ''}
                </Badge>
              )}
              {disabledCount > 0 && (
                <Badge className="bg-slate-50 text-slate-500 border-slate-200 hover:bg-slate-50">
                  {disabledCount} disabled
                </Badge>
              )}
            </div>

            {hooks.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-16 text-center">
                <div className="h-16 w-16 rounded-2xl bg-slate-100 flex items-center justify-center mb-4">
                  <Zap className="h-7 w-7 text-slate-400" />
                </div>
                <h3 className="text-lg font-semibold mb-1">No hooks found</h3>
                <p className="text-sm text-muted-foreground max-w-md">
                  Create Python files in{' '}
                  <code className="text-xs bg-slate-100 px-1.5 py-0.5 rounded font-mono">
                    {hooksDir}
                  </code>{' '}
                  with a <code className="text-xs bg-slate-100 px-1.5 py-0.5 rounded font-mono">register(pb)</code>{' '}
                  function to add hooks. They will be discovered automatically.
                </p>
              </div>
            ) : (
              <div className="grid gap-3">
                {hooks.map((hook) => (
                  <HookCard
                    key={hook.id}
                    hook={hook}
                    onToggle={handleToggle}
                    onReload={handleReload}
                    toggling={togglingId === hook.id}
                    reloading={reloadingId === hook.id}
                  />
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </>
  )
}
