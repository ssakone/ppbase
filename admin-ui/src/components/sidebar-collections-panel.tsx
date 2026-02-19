import { useState, useMemo, useDeferredValue } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { useCollections } from '@/hooks/use-collections'
import { useSidebar } from '@/context/sidebar-context'
import { cn } from '@/lib/utils'
import { prefetchRoute } from '@/lib/route-prefetch'
import { navigateWithTransition } from '@/lib/navigation'
import { Plus, Search, ChevronRight, Folder, User, Table2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import type { Collection } from '@/api/types'

export function SidebarCollectionsPanel() {
  const navigate = useNavigate()
  const location = useLocation()
  const { data: collections = [], isLoading } = useCollections()
  const { setSelectedCollectionId, setActiveSection, setSidebarOpen, collectionsPanelWidth } = useSidebar()
  const [search, setSearch] = useState('')
  const deferredSearch = useDeferredValue(search)
  const [systemCollapsed, setSystemCollapsed] = useState(false)

  const filtered = useMemo(() => {
    if (!deferredSearch) return collections
    const q = deferredSearch.toLowerCase()
    return collections.filter((c: Collection) => c.name.toLowerCase().includes(q))
  }, [collections, deferredSearch])

  const userCollections = useMemo(
    () => filtered.filter((c: Collection) => !c.system),
    [filtered],
  )

  const systemCollections = useMemo(
    () => filtered.filter((c: Collection) => c.system),
    [filtered],
  )

  const handleClick = (col: typeof collections[0]) => {
    setActiveSection('collections')
    setSelectedCollectionId(col.id)
    setSidebarOpen(false)
    const path = `/collections/${col.id}`
    if (location.pathname === path) {
      return
    }
    navigateWithTransition(navigate, path)
  }

  const handleNewCollection = () => {
    setSidebarOpen(false)
    navigateWithTransition(navigate, '/collections?new=1')
  }

  const showPanel = location.pathname.startsWith('/collections')
  if (!showPanel) return null

  const renderCollectionItem = (col: Collection) => {
    const isActive = location.pathname === `/collections/${col.id}`
    return (
      <button
        key={col.id}
        className={cn(
          'flex items-center gap-3 w-full px-3.5 mb-0.5 py-2.5 text-[15px] rounded-xl text-left transition-all duration-150',
          isActive
            ? 'bg-indigo-50 text-indigo-700 font-semibold shadow-sm'
            : 'text-slate-600 hover:bg-slate-100 hover:text-slate-800 hover:translate-x-[1px]',
        )}
        onClick={() => handleClick(col)}
        onMouseEnter={() => prefetchRoute('records')}
        onFocus={() => prefetchRoute('records')}
      >
        <CollectionIcon type={col.type} active={isActive} />
        <span className="truncate">{col.name}</span>
      </button>
    )
  }

  return (
    <div
      className="flex flex-col border-r bg-white shrink-0 transition-all duration-200"
      style={{ width: collectionsPanelWidth }}
    >
      {/* Search */}
      <div className="px-4 pt-4 pb-3">
        <div className="relative">
          <Search className="absolute left-3.5 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground pointer-events-none" />
          <input
            type="text"
            className="w-full h-11 pl-10 pr-3.5 text-[15px] rounded-xl border bg-slate-50/60 placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring focus:bg-white hover:border-slate-300 transition-all duration-150"
            placeholder="Search collections..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
      </div>

      {/* Collection list */}
      <nav className="flex-1 overflow-y-auto px-3 py-1">
        {isLoading ? (
          <div className="space-y-1.5 px-1 py-1">
            {[1, 2, 3, 4].map((i) => (
              <div key={i} className="h-10 rounded-xl skeleton-shimmer" />
            ))}
          </div>
        ) : filtered.length === 0 ? (
          <div className="px-3 py-4 text-[14px] text-muted-foreground text-center">
            {search ? 'No matching collections' : 'No collections yet'}
          </div>
        ) : (
          <>
            {userCollections.map(renderCollectionItem)}

            {systemCollections.length > 0 && (
              <div className="mt-3 pt-3 border-t border-slate-100">
                <button
                  className="flex items-center gap-1.5 w-full px-3 py-1.5 text-[11px] font-bold uppercase tracking-widest text-slate-400 hover:text-slate-500 transition-colors mb-1"
                  onClick={() => setSystemCollapsed(!systemCollapsed)}
                >
                  System
                  <ChevronRight
                    className={cn(
                      'h-3 w-3 transition-transform duration-200',
                      !systemCollapsed && 'rotate-90',
                    )}
                  />
                </button>
                {!systemCollapsed && systemCollections.map(renderCollectionItem)}
              </div>
            )}
          </>
        )}
      </nav>

      {/* New collection button */}
      <div className="px-4 pb-4 pt-2">
        <Button
          variant="outline"
          className="w-full border-dashed border-slate-300 text-slate-500 hover:text-indigo-600 hover:border-indigo-400 hover:bg-indigo-50/50"
          onClick={handleNewCollection}
          onMouseEnter={() => prefetchRoute('collections')}
          onFocus={() => prefetchRoute('collections')}
        >
          <Plus className="h-4 w-4" />
          New collection
        </Button>
      </div>
    </div>
  )
}

function CollectionIcon({ type, active }: { type: string; active?: boolean }) {
  const cls = cn(
    'h-[18px] w-[18px] shrink-0 transition-colors',
    active ? 'text-indigo-500' : 'text-slate-400',
  )
  if (type === 'auth') return <User className={cls} />
  if (type === 'view') return <Table2 className={cls} />
  return <Folder className={cls} />
}
