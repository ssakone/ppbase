import { Navigate, Outlet } from 'react-router-dom'
import { useAuth } from '@/context/auth-context'
import { useSidebar } from '@/context/sidebar-context'
import { AppSidebar } from '@/components/app-sidebar'
import { LoadingSpinner } from '@/components/loading-spinner'
import { Menu } from 'lucide-react'
import { Button } from '@/components/ui/button'

export function Layout() {
  const { isAuthenticated, isLoading, needsSetup } = useAuth()
  const { setSidebarOpen } = useSidebar()

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
          <span className="font-semibold text-foreground truncate">PPBase</span>
        </header>
        <main className="flex-1 flex flex-col overflow-hidden min-w-0">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
