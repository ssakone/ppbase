import { useState, useEffect } from 'react'
import { useSettings, useUpdateSettings } from '@/hooks/use-settings'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { LoadingSpinner } from '@/components/loading-spinner'
import { toast } from 'sonner'

export function SettingsForm() {
  const { data: settings } = useSettings()
  const updateSettings = useUpdateSettings()

  const [appName, setAppName] = useState('')
  const [appUrl, setAppUrl] = useState('')

  useEffect(() => {
    if (settings) {
      setAppName(settings.meta?.appName ?? '')
      setAppUrl(settings.meta?.appUrl ?? '')
    }
  }, [settings])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      await updateSettings.mutateAsync({
        meta: { appName, appUrl },
      })
      toast.success('Settings saved successfully.')
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to save settings.'
      toast.error(message)
    }
  }

  return (
    <div className="max-w-2xl">
      <div className="rounded-lg border bg-white">
        <div className="border-b px-6 py-4">
          <h2 className="text-lg font-semibold">Application Settings</h2>
          <p className="text-sm text-muted-foreground">Configure your PPBase instance.</p>
        </div>
        <form onSubmit={handleSubmit} className="space-y-4 px-6 py-4">
          <div className="space-y-2">
            <Label htmlFor="appName">Application Name</Label>
            <Input
              id="appName"
              value={appName}
              onChange={(e) => setAppName(e.target.value)}
              placeholder="My App"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="appUrl">Application URL</Label>
            <Input
              id="appUrl"
              type="url"
              value={appUrl}
              onChange={(e) => setAppUrl(e.target.value)}
              placeholder="https://example.com"
            />
          </div>
          <div className="pt-2">
            <Button type="submit" disabled={updateSettings.isPending}>
              {updateSettings.isPending && <LoadingSpinner size="sm" className="mr-2" />}
              Save Changes
            </Button>
          </div>
        </form>
      </div>
    </div>
  )
}
