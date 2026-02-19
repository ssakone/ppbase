import { lazy, Suspense, type ReactElement } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Toaster } from 'sonner'
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
        <BrowserRouter basename="/_/">
          <CommandPaletteProvider>
            <SidebarProvider>
              <Toaster position="bottom-right" richColors closeButton />
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
