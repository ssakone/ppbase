import { Navigate, Outlet } from 'react-router-dom'
import { useAuth } from '@/context/auth-context'
import { AppSidebar } from '@/components/app-sidebar'
import { LoadingSpinner } from '@/components/loading-spinner'

export function Layout() {
  const { isAuthenticated, isLoading } = useAuth()

  if (isLoading) {
    return <LoadingSpinner fullPage size="lg" />
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" replace />
  }

  return (
    <div className="flex h-screen overflow-hidden bg-slate-50">
      <AppSidebar />
      <main className="flex-1 flex flex-col overflow-hidden">
        <Outlet />
      </main>
    </div>
  )
}
