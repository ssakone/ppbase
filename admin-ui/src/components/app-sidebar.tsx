import { useRef, useCallback } from 'react'
import { useLocation } from 'react-router-dom'
import { SidebarIconStrip } from './sidebar-icon-strip'
import { SidebarCollectionsPanel } from './sidebar-collections-panel'
import { Sheet, SheetContent } from '@/components/ui/sheet'
import { useSidebar } from '@/context/sidebar-context'

function SidebarContent() {
  return (
    <div className="flex h-full">
      <SidebarIconStrip />
      <SidebarCollectionsPanel />
    </div>
  )
}

function SidebarResizeHandle() {
  const { collectionsPanelWidth, setCollectionsPanelWidth } = useSidebar()
  const startXRef = useRef<number>(0)
  const startWidthRef = useRef<number>(0)

  const handleMouseDown = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault()
      startXRef.current = e.clientX
      startWidthRef.current = collectionsPanelWidth

      const handleMouseMove = (e: MouseEvent) => {
        const dx = e.clientX - startXRef.current
        setCollectionsPanelWidth(startWidthRef.current + dx)
      }
      const handleMouseUp = () => {
        document.removeEventListener('mousemove', handleMouseMove)
        document.removeEventListener('mouseup', handleMouseUp)
        document.body.style.cursor = ''
        document.body.style.userSelect = ''
      }
      document.addEventListener('mousemove', handleMouseMove)
      document.addEventListener('mouseup', handleMouseUp)
      document.body.style.cursor = 'col-resize'
      document.body.style.userSelect = 'none'
    },
    [collectionsPanelWidth, setCollectionsPanelWidth],
  )

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-valuenow={collectionsPanelWidth}
      onMouseDown={handleMouseDown}
      className="w-1 shrink-0 cursor-col-resize border-r border-transparent hover:border-slate-200 hover:bg-slate-100 group transition-colors flex items-center justify-center"
      title="Drag to resize"
    >
      <div className="w-0.5 h-8 rounded-full bg-slate-300 opacity-0 group-hover:opacity-100 transition-opacity" />
    </div>
  )
}

export function AppSidebar() {
  const { sidebarOpen, setSidebarOpen } = useSidebar()
  const location = useLocation()
  const showResizeHandle = location.pathname.startsWith('/collections')

  return (
    <>
      {/* Desktop: always visible with resize handle when on collections */}
      <aside className="hidden md:flex h-screen shrink-0">
        <SidebarContent />
        {showResizeHandle && <SidebarResizeHandle />}
      </aside>

      {/* Mobile: sheet overlay */}
      <Sheet open={sidebarOpen} onOpenChange={setSidebarOpen}>
        <SheetContent
          side="left"
          className="w-[375px] max-w-[85vw] p-0 border-0"
        >
          <div className="h-full pt-14">
            <SidebarContent />
          </div>
        </SheetContent>
      </Sheet>
    </>
  )
}
