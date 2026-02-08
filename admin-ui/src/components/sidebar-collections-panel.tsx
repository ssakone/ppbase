import { useState, useMemo } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { useCollections } from '@/hooks/use-collections'
import { useSidebar } from '@/context/sidebar-context'
import { cn } from '@/lib/utils'
import { Plus, Search, ChevronRight } from 'lucide-react'
import { Button } from '@/components/ui/button'
import type { Collection } from '@/api/types'

export function SidebarCollectionsPanel() {
  const navigate = useNavigate()
  const location = useLocation()
  const { data: collections = [], isLoading } = useCollections()
  const { setSelectedCollectionId, setActiveSection } = useSidebar()
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
    navigate(`/collections/${col.id}`)
  }

  const handleNewCollection = () => {
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
          'flex items-center gap-2.5 w-full px-3 mb-1 py-2 text-[16px] rounded-md text-left transition-colors',
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
    <div className="flex flex-col w-[300px] border-r bg-white">
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
              <div className="mt-3">
                <button
                  className="flex items-center gap-1.5 w-full px-3 py-1.5 text-xs font-semibold uppercase tracking-wider text-slate-400 hover:text-slate-600 transition-colors"
                  onClick={() => setSystemCollapsed(!systemCollapsed)}
                >
                  <ChevronRight
                    className={cn(
                      'h-3.5 w-3.5 transition-transform',
                      !systemCollapsed && 'rotate-90',
                    )}
                  />
                  System
                </button>
                {!systemCollapsed && systemCollections.map(renderCollectionItem)}
              </div>
            )}
          </>
        )}
      </nav>

      {/* New collection button */}
      <div className="px-4 pb-3 pt-2">
        <Button
          variant="outline"
          className="w-full border-solid border-black h-10"
          onClick={handleNewCollection}
        >
          <Plus className="h-4 w-4" />
          New collection
        </Button>
      </div>
    </div>
  )
}

function CollectionIcon({ type }: { type: string }) {
  if (type === 'auth') {
    return (
      <svg width="18" height="18" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className="shrink-0 text-slate-400">
        <circle cx="8" cy="6" r="3"/>
        <path d="M2 14c0-3.3 2.7-5 6-5s6 1.7 6 5"/>
      </svg>
    )
  }
  if (type === 'view') {
    return (
      <svg width="18" height="18" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className="shrink-0 text-slate-400">
        <rect x="2" y="2" width="5" height="5" rx="0.5"/>
        <rect x="9" y="2" width="5" height="5" rx="0.5"/>
        <rect x="2" y="9" width="5" height="5" rx="0.5"/>
        <rect x="9" y="9" width="5" height="5" rx="0.5"/>
      </svg>
    )
  }
  return (
    <svg width="18" height="18" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className="shrink-0 text-slate-400">
      <path d="M9 2H4a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1V6L9 2z"/>
      <path d="M9 2v4h4"/>
    </svg>
  )
}
