import { useEffect } from 'react'
import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { useAuth } from '@/context/auth-context'
import { useCommandPalette } from '@/context/command-palette-context'
import { useSidebar } from '@/context/sidebar-context'
import { AppSidebar } from '@/components/app-sidebar'
import { LoadingSpinner } from '@/components/loading-spinner'
import { prefetchRoutes } from '@/lib/route-prefetch'
import { Command, Menu } from 'lucide-react'
import { Button } from '@/components/ui/button'

export function Layout() {
  const { isAuthenticated, isLoading, needsSetup } = useAuth()
  const { setSidebarOpen } = useSidebar()
  const { openPalette } = useCommandPalette()
  const location = useLocation()

  useEffect(() => {
    if (!isAuthenticated) {
      return
    }

    const warmRoutes = () => {
      prefetchRoutes(['dashboard', 'collections', 'records', 'migrations', 'logs', 'settings'])
    }

    if (typeof window === 'undefined') {
      return
    }

    const ric = window.requestIdleCallback as
      | ((callback: IdleRequestCallback, options?: IdleRequestOptions) => number)
      | undefined
    const cic = window.cancelIdleCallback as ((handle: number) => void) | undefined

    if (ric) {
      const handle = ric(() => warmRoutes(), { timeout: 900 })
      return () => {
        if (cic) {
          cic(handle)
        }
      }
    }

    const timeout = window.setTimeout(warmRoutes, 160)
    return () => {
      window.clearTimeout(timeout)
    }
  }, [isAuthenticated])

  const sectionTitle = (() => {
    if (location.pathname === '/dashboard') return 'Dashboard'
    if (location.pathname.startsWith('/collections')) return 'Collections'
    if (location.pathname === '/migrations') return 'Migrations'
    if (location.pathname === '/logs') return 'Logs'
    if (location.pathname === '/settings') return 'Settings'
    return 'PPBase'
  })()

  if (isLoading) {
    return <LoadingSpinner fullPage size="lg" />
  }

  if (!isAuthenticated) {
    if (needsSetup) {
      return <Navigate to="/setup" replace />
    }
    return <Navigate to="/login" replace />
  }

  return (
    <div className="flex h-screen overflow-hidden bg-slate-50">
      <AppSidebar />
      <div className="flex-1 flex flex-col overflow-hidden min-w-0">
        {/* Mobile header with hamburger */}
        <header className="md:hidden flex items-center gap-3 px-4 py-3 border-b bg-white shrink-0">
          <Button
            variant="ghost"
            size="icon"
            className="shrink-0"
            onClick={() => setSidebarOpen(true)}
            aria-label="Open menu"
          >
            <Menu className="h-6 w-6" />
          </Button>
          <span className="font-semibold text-foreground truncate">{sectionTitle}</span>
          <Button
            variant="ghost"
            size="icon"
            className="ml-auto shrink-0"
            onClick={openPalette}
            aria-label="Quick actions"
          >
            <Command className="h-5 w-5" />
          </Button>
        </header>
        <main className="flex-1 flex flex-col overflow-hidden min-w-0">
          <div key={location.pathname} className="flex flex-1 min-h-0 flex-col animate-page-swap">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  )
}
