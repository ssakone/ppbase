import { lazy, Suspense, type ReactElement } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Toaster } from 'sonner'
import { CheckCircle2, CircleAlert } from 'lucide-react'
import { AuthProvider } from '@/context/auth-context'
import { CommandPaletteProvider } from '@/context/command-palette-context'
import { SidebarProvider } from '@/context/sidebar-context'
import { CommandPalette } from '@/components/command-palette'
import { RouteTransitionFallback } from '@/components/route-transition-fallback'

const Layout = lazy(async () => {
  const module = await import('@/routes/layout')
  return { default: module.Layout }
})

const LoginPage = lazy(async () => {
  const module = await import('@/routes/login')
  return { default: module.LoginPage }
})

const SetupPage = lazy(async () => {
  const module = await import('@/routes/setup')
  return { default: module.SetupPage }
})

const DashboardPage = lazy(async () => {
  const module = await import('@/routes/dashboard')
  return { default: module.DashboardPage }
})

const CollectionsPage = lazy(async () => {
  const module = await import('@/routes/collections/index')
  return { default: module.CollectionsPage }
})

const RecordsPage = lazy(async () => {
  const module = await import('@/routes/collections/[id]')
  return { default: module.RecordsPage }
})

const MigrationsPage = lazy(async () => {
  const module = await import('@/routes/migrations')
  return { default: module.MigrationsPage }
})

const SettingsPage = lazy(async () => {
  const module = await import('@/routes/settings')
  return { default: module.SettingsPage }
})

const LogsPage = lazy(async () => {
  const module = await import('@/routes/logs')
  return { default: module.LogsPage }
})

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      staleTime: 30_000,
      refetchOnWindowFocus: false,
    },
  },
})

function withSuspense(element: ReactElement) {
  return <Suspense fallback={<RouteTransitionFallback />}>{element}</Suspense>
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <BrowserRouter basename="/_">
          <CommandPaletteProvider>
            <SidebarProvider>
              <Toaster
                position="bottom-center"
                closeButton
                toastOptions={{
                  classNames: {
                    toast:
                      'w-[min(560px,calc(100vw-2rem))] rounded-xl border border-slate-200 bg-white text-slate-900 shadow-lg',
                    icon: 'order-1 shrink-0',
                    content: 'order-2 min-w-0 flex-1',
                    title: 'text-sm font-medium',
                    description: 'text-xs text-slate-600',
                    closeButton:
                      '!static !top-auto !left-auto !right-auto !translate-x-0 !translate-y-0 order-3 ml-auto shrink-0 border border-slate-200 bg-white text-slate-500 hover:bg-slate-100 hover:text-slate-700',
                    success:
                      '!border-emerald-200 !bg-emerald-50/90 [&_[data-icon]]:text-emerald-600',
                    error:
                      '!border-red-200 !bg-red-50/90 [&_[data-icon]]:text-red-600',
                  },
                }}
                icons={{
                  success: <CheckCircle2 className="h-4 w-4" />,
                  error: <CircleAlert className="h-4 w-4" />,
                }}
              />
              <Routes>
                <Route path="/login" element={withSuspense(<LoginPage />)} />
                <Route path="/setup" element={withSuspense(<SetupPage />)} />
                <Route element={withSuspense(<Layout />)}>
                  <Route index element={<Navigate to="/dashboard" replace />} />
                  <Route path="/dashboard" element={withSuspense(<DashboardPage />)} />
                  <Route path="/collections" element={withSuspense(<CollectionsPage />)} />
                  <Route path="/collections/:id" element={withSuspense(<RecordsPage />)} />
                  <Route path="/migrations" element={withSuspense(<MigrationsPage />)} />
                  <Route path="/logs" element={withSuspense(<LogsPage />)} />
                  <Route path="/settings" element={withSuspense(<SettingsPage />)} />
                </Route>
                <Route path="*" element={<Navigate to="/dashboard" replace />} />
              </Routes>
              <CommandPalette />
            </SidebarProvider>
          </CommandPaletteProvider>
        </BrowserRouter>
      </AuthProvider>
    </QueryClientProvider>
  )
}
