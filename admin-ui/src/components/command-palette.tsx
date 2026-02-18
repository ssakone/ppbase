import { useEffect, useMemo, useState, type ElementType, type KeyboardEvent } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import {
  Activity,
  ChevronRight,
  Database,
  GitBranch,
  Home,
  LayoutGrid,
  LogOut,
  Plus,
  Search,
  Settings,
  ShieldCheck,
} from 'lucide-react'
import { useCommandPalette } from '@/context/command-palette-context'
import { useAuth } from '@/context/auth-context'
import { useCollections } from '@/hooks/use-collections'
import { Dialog, DialogContent } from '@/components/ui/dialog'
import { cn } from '@/lib/utils'
import { navigateWithTransition } from '@/lib/navigation'
import { prefetchRoute, type PrefetchKey } from '@/lib/route-prefetch'

type CommandItem = {
  id: string
  label: string
  hint?: string
  group: string
  keywords: string[]
  icon: ElementType
  prefetch?: PrefetchKey
  run: () => void
}

export function CommandPalette() {
  const navigate = useNavigate()
  const location = useLocation()
  const { open, closePalette } = useCommandPalette()
  const { logout } = useAuth()
  const { data: collections = [] } = useCollections()

  const [query, setQuery] = useState('')
  const [activeIndex, setActiveIndex] = useState(0)

  const sortedCollections = useMemo(
    () =>
      [...collections]
        .sort((a, b) => new Date(b.updated).getTime() - new Date(a.updated).getTime())
        .slice(0, 6),
    [collections],
  )

  const items = useMemo<CommandItem[]>(() => {
    const go = (path: string) => () => {
      if (location.pathname !== path) {
        navigateWithTransition(navigate, path)
      }
      closePalette()
    }

    const baseItems: CommandItem[] = [
      {
        id: 'go-dashboard',
        label: 'Go to Dashboard',
        hint: 'Navigation',
        group: 'Navigation',
        keywords: ['home', 'overview', 'stats'],
        icon: Home,
        prefetch: 'dashboard',
        run: go('/dashboard'),
      },
      {
        id: 'go-collections',
        label: 'Go to Collections',
        hint: 'Navigation',
        group: 'Navigation',
        keywords: ['schema', 'records', 'data'],
        icon: LayoutGrid,
        prefetch: 'collections',
        run: go('/collections'),
      },
      {
        id: 'go-migrations',
        label: 'Go to Migrations',
        hint: 'Navigation',
        group: 'Navigation',
        keywords: ['database', 'migration', 'schema'],
        icon: GitBranch,
        prefetch: 'migrations',
        run: go('/migrations'),
      },
      {
        id: 'go-logs',
        label: 'Go to Logs',
        hint: 'Navigation',
        group: 'Navigation',
        keywords: ['errors', 'requests', 'monitoring'],
        icon: Activity,
        prefetch: 'logs',
        run: go('/logs'),
      },
      {
        id: 'go-settings',
        label: 'Go to Settings',
        hint: 'Navigation',
        group: 'Navigation',
        keywords: ['config', 'preferences', 'smtp'],
        icon: Settings,
        prefetch: 'settings',
        run: go('/settings'),
      },
      {
        id: 'new-collection',
        label: 'Create New Collection',
        hint: 'Action',
        group: 'Actions',
        keywords: ['create', 'new', 'collection'],
        icon: Plus,
        prefetch: 'collections',
        run: () => {
          navigateWithTransition(navigate, '/collections?new=1')
          closePalette()
        },
      },
      {
        id: 'superusers',
        label: 'Open Superusers',
        hint: 'Action',
        group: 'Actions',
        keywords: ['admin', 'auth', 'users', 'permissions'],
        icon: ShieldCheck,
        prefetch: 'records',
        run: () => {
          navigateWithTransition(navigate, '/collections/_superusers')
          closePalette()
        },
      },
      {
        id: 'logout',
        label: 'Log Out',
        hint: 'Session',
        group: 'Session',
        keywords: ['sign out', 'exit'],
        icon: LogOut,
        run: () => {
          logout()
          closePalette()
        },
      },
    ]

    const collectionItems: CommandItem[] = sortedCollections.map((collection) => ({
      id: `collection-${collection.id}`,
      label: `Open Collection: ${collection.name}`,
      hint: collection.type,
      group: 'Recent Collections',
      keywords: [collection.name, collection.type, 'collection'],
      icon: Database,
      prefetch: 'records',
      run: () => {
        navigateWithTransition(navigate, `/collections/${collection.id}`)
        closePalette()
      },
    }))

    return [...baseItems, ...collectionItems]
  }, [closePalette, location.pathname, logout, navigate, sortedCollections])

  const filteredItems = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) {
      return items
    }

    return items.filter((item) => {
      const haystack = [item.label, item.group, item.hint, ...item.keywords]
        .filter(Boolean)
        .join(' ')
        .toLowerCase()
      return haystack.includes(q)
    })
  }, [items, query])

  useEffect(() => {
    if (!open) {
      setQuery('')
      setActiveIndex(0)
      return
    }
    setActiveIndex(0)
  }, [open])

  useEffect(() => {
    setActiveIndex(0)
  }, [query])

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setActiveIndex((prev) => (filteredItems.length === 0 ? 0 : (prev + 1) % filteredItems.length))
      return
    }

    if (event.key === 'ArrowUp') {
      event.preventDefault()
      setActiveIndex((prev) => (filteredItems.length === 0 ? 0 : (prev - 1 + filteredItems.length) % filteredItems.length))
      return
    }

    if (event.key === 'Enter') {
      event.preventDefault()
      const selected = filteredItems[activeIndex]
      if (selected) {
        selected.run()
      }
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => (!next ? closePalette() : null)}>
      <DialogContent className="max-w-2xl p-0 overflow-hidden">
        <div className="border-b px-4 py-3">
          <label className="flex items-center gap-2 rounded-lg border bg-slate-50 px-3 py-2.5">
            <Search className="h-4 w-4 text-muted-foreground" />
            <input
              autoFocus
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={onKeyDown}
              placeholder="Search pages, collections, and actions..."
              className="w-full bg-transparent text-sm outline-none placeholder:text-muted-foreground"
            />
            <kbd className="rounded border bg-white px-2 py-0.5 text-xs text-muted-foreground">Esc</kbd>
          </label>
          <p className="mt-2 text-xs text-muted-foreground">
            Quick actions:
            {' '}
            <span className="font-medium">Ctrl/Cmd + K</span>
          </p>
        </div>

        <div className="max-h-[460px] overflow-y-auto p-2">
          {filteredItems.length === 0 ? (
            <div className="rounded-lg px-3 py-8 text-center text-sm text-muted-foreground">
              No matching command.
            </div>
          ) : (
            filteredItems.map((item, index) => (
              <button
                key={item.id}
                type="button"
                onClick={item.run}
                onMouseEnter={() => {
                  setActiveIndex(index)
                  if (item.prefetch) {
                    prefetchRoute(item.prefetch)
                  }
                }}
                onFocus={() => {
                  setActiveIndex(index)
                  if (item.prefetch) {
                    prefetchRoute(item.prefetch)
                  }
                }}
                className={cn(
                  'flex w-full items-center justify-between rounded-lg px-3 py-2.5 text-left transition-colors',
                  index === activeIndex
                    ? 'bg-indigo-50 text-indigo-700'
                    : 'hover:bg-slate-100 text-slate-700',
                )}
              >
                <span className="flex items-center gap-2.5 min-w-0">
                  <span className={cn(
                    'inline-flex h-8 w-8 items-center justify-center rounded-md border shrink-0',
                    index === activeIndex ? 'border-indigo-200 bg-white text-indigo-600' : 'border-slate-200 bg-white text-slate-500',
                  )}>
                    <item.icon className="h-4 w-4" />
                  </span>
                  <span className="min-w-0">
                    <span className="block truncate text-sm font-medium">{item.label}</span>
                    <span className="block truncate text-xs text-muted-foreground">{item.group}</span>
                  </span>
                </span>

                <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                  {item.hint && <span className="rounded border bg-white px-1.5 py-0.5">{item.hint}</span>}
                  <ChevronRight className="h-3.5 w-3.5" />
                </span>
              </button>
            ))
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}
