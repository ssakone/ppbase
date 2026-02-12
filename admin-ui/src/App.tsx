import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Toaster } from 'sonner'
import { AuthProvider } from '@/context/auth-context'
import { SidebarProvider } from '@/context/sidebar-context'
import { Layout } from '@/routes/layout'
import { LoginPage } from '@/routes/login'
import { SetupPage } from '@/routes/setup'
import { CollectionsPage } from '@/routes/collections/index'
import { RecordsPage } from '@/routes/collections/[id]'
import { MigrationsPage } from '@/routes/migrations'
import { SettingsPage } from '@/routes/settings'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
})

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <BrowserRouter basename="/_/">
          <SidebarProvider>
            <Toaster position="bottom-right" richColors closeButton />
            <Routes>
              <Route path="/login" element={<LoginPage />} />
              <Route path="/setup" element={<SetupPage />} />
              <Route element={<Layout />}>
                <Route index element={<Navigate to="/collections" replace />} />
                <Route path="/collections" element={<CollectionsPage />} />
                <Route path="/collections/:id" element={<RecordsPage />} />
                <Route path="/migrations" element={<MigrationsPage />} />
                <Route path="/settings" element={<SettingsPage />} />
              </Route>
              <Route path="*" element={<Navigate to="/collections" replace />} />
            </Routes>
          </SidebarProvider>
        </BrowserRouter>
      </AuthProvider>
    </QueryClientProvider>
  )
}
