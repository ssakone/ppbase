import { useSettings } from '@/hooks/use-settings'
import { ContentHeader } from '@/components/content-header'
import { Breadcrumb } from '@/components/breadcrumb'
import { SettingsForm } from '@/components/settings-form'
import { LoadingSpinner } from '@/components/loading-spinner'
import { Button } from '@/components/ui/button'
import { AlertCircle } from 'lucide-react'

export function SettingsPage() {
  const { isLoading, isError, refetch } = useSettings()

  return (
    <>
      <ContentHeader
        left={<Breadcrumb items={[{ label: 'Settings', active: true }]} />}
      />
      <div className="flex-1 overflow-auto p-6">
        {isLoading ? (
          <LoadingSpinner fullPage />
        ) : isError ? (
          <div className="flex flex-col items-center justify-center py-16 text-center">
            <AlertCircle className="h-10 w-10 text-destructive mb-3" />
            <h3 className="text-lg font-semibold mb-1">Failed to load settings</h3>
            <p className="text-sm text-muted-foreground mb-4">
              Could not connect to the settings API.
            </p>
            <Button variant="outline" onClick={() => refetch()}>
              Retry
            </Button>
          </div>
        ) : (
          <SettingsForm />
        )}
      </div>
    </>
  )
}
