import { useState, useMemo } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { useCollections } from '@/hooks/use-collections'
import { useSidebar } from '@/context/sidebar-context'
import { cn } from '@/lib/utils'
import { Plus, Search, ChevronRight, Folder, User, Table2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import type { Collection } from '@/api/types'

export function SidebarCollectionsPanel() {
  const navigate = useNavigate()
  const location = useLocation()
  const { data: collections = [], isLoading } = useCollections()
  const { setSelectedCollectionId, setActiveSection, setSidebarOpen, collectionsPanelWidth } = useSidebar()
  const [search, setSearch] = useState('')
  const [systemCollapsed, setSystemCollapsed] = useState(false)

  const filtered = useMemo(() => {
    if (!search) return collections
    const q = search.toLowerCase()
    return collections.filter((c: Collection) => c.name.toLowerCase().includes(q))
  }, [collections, search])

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
    navigate(`/collections/${col.id}`)
  }

  const handleNewCollection = () => {
    setSidebarOpen(false)
    navigate('/collections?new=1')
  }

  // Hide panel when not on collections routes
  const showPanel = location.pathname.startsWith('/collections')
  if (!showPanel) return null

  const renderCollectionItem = (col: Collection) => {
    const isActive = location.pathname === `/collections/${col.id}`
    return (
      <button
        key={col.id}
        className={cn(
          'flex items-center gap-2.5 w-full px-3 mb-0.5 py-1.5 text-[13.5px] rounded-md text-left transition-colors',
          isActive
            ? 'bg-indigo-50 text-indigo-700 font-medium'
            : 'text-slate-600 hover:bg-slate-100',
        )}
        onClick={() => handleClick(col)}
      >
        <CollectionIcon type={col.type} />
        <span className="truncate">{col.name}</span>
      </button>
    )
  }

  return (
    <div
      className="flex flex-col border-r bg-white shrink-0"
      style={{ width: collectionsPanelWidth }}
    >
      {/* Search */}
      <div className="px-4 pt-3 pb-2">
        <div className="relative">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <input
            type="text"
            className="w-full h-9 pl-9 pr-3 text-sm rounded-md border bg-background placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
            placeholder="Search collections..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
      </div>

      {/* Collection list */}
      <nav className="flex-1 overflow-y-auto px-3 py-1">
        {isLoading ? (
          <div className="space-y-2 px-2 py-1">
            {[1, 2, 3].map((i) => (
              <div key={i} className="h-8 rounded-md bg-slate-100 animate-pulse" />
            ))}
          </div>
        ) : filtered.length === 0 ? (
          <div className="px-3 py-3 text-xs text-muted-foreground">
            {search ? 'No matching collections' : 'No collections yet'}
          </div>
        ) : (
          <>
            {/* User collections */}
            {userCollections.map(renderCollectionItem)}

            {/* System collections section */}
            {systemCollections.length > 0 && (
              <div className="mt-2 pt-2 border-t border-slate-100">
                <button
                  className="flex items-center gap-1 w-full px-3 py-1 text-[11px] font-medium uppercase tracking-wider text-slate-400 hover:text-slate-500 transition-colors"
                  onClick={() => setSystemCollapsed(!systemCollapsed)}
                >
                  System
                  <ChevronRight
                    className={cn(
                      'h-3 w-3 transition-transform',
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
      <div className="px-3 pb-3 pt-2">
        <Button
          variant="outline"
          className="w-full border-dashed border-slate-300 text-slate-500 hover:text-slate-700 hover:border-slate-400 h-9 text-[13px]"
          onClick={handleNewCollection}
        >
          <Plus className="h-3.5 w-3.5 mr-1" />
          New collection
        </Button>
      </div>
    </div>
  )
}

function CollectionIcon({ type }: { type: string }) {
  if (type === 'auth') {
    return <User className="h-4 w-4 shrink-0 text-slate-400" />
  }
  if (type === 'view') {
    return <Table2 className="h-4 w-4 shrink-0 text-slate-400" />
  }
  return <Folder className="h-4 w-4 shrink-0 text-slate-400" />
}
