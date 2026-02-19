import { useState, useEffect } from 'react'
import { useSettings, useUpdateSettings } from '@/hooks/use-settings'
import { testSettingsEmail, type TestEmailTemplate } from '@/api/endpoints/settings'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Checkbox } from '@/components/ui/checkbox'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { LoadingSpinner } from '@/components/loading-spinner'
import { toast } from 'sonner'

type SettingsData = Record<string, Record<string, unknown>>
type SaveHandler = (d: SettingsData) => Promise<void>

function getErrorMessage(error: unknown, fallback: string): string {
  if (error && typeof error === 'object') {
    const maybe = error as { message?: unknown }
    if (typeof maybe.message === 'string' && maybe.message.trim()) {
      return maybe.message
    }
  }
  if (error instanceof Error && error.message.trim()) {
    return error.message
  }
  return fallback
}

function SectionCard({ title, description, children }: { title: string; description?: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border bg-white max-w-2xl">
      <div className="border-b px-6 py-4">
        <h2 className="text-base font-semibold">{title}</h2>
        {description && <p className="text-sm text-muted-foreground">{description}</p>}
      </div>
      <div className="space-y-4 px-6 py-4">{children}</div>
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <Label className="text-sm">{label}</Label>
      {children}
    </div>
  )
}

// ── Meta Tab ──────────────────────────────────────────────────────────────────
function MetaTab({ settings, onSave, saving }: { settings: SettingsData; onSave: SaveHandler; saving: boolean }) {
  const [appName, setAppName] = useState('')
  const [appUrl, setAppUrl] = useState('')
  const [supportEmail, setSupportEmail] = useState('')
  const [senderName, setSenderName] = useState('')
  const [senderAddress, setSenderAddress] = useState('')

  useEffect(() => {
    const m = settings.meta ?? {}
    setAppName(String(m.appName ?? ''))
    setAppUrl(String(m.appUrl ?? ''))
    setSupportEmail(String(m.supportEmail ?? ''))
    setSenderName(String(m.senderName ?? ''))
    setSenderAddress(String(m.senderAddress ?? ''))
  }, [settings])

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    void onSave({ meta: { appName, appUrl, supportEmail, senderName, senderAddress } })
  }

  return (
    <SectionCard title="Application" description="General settings for your PPBase instance.">
      <form onSubmit={handleSubmit} className="space-y-4">
        <Field label="Application name"><Input value={appName} onChange={(e) => setAppName(e.target.value)} placeholder="My App" /></Field>
        <Field label="Application URL"><Input type="url" value={appUrl} onChange={(e) => setAppUrl(e.target.value)} placeholder="https://example.com" /></Field>
        <Field label="Support email"><Input type="email" value={supportEmail} onChange={(e) => setSupportEmail(e.target.value)} placeholder="support@example.com" /></Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Sender name"><Input value={senderName} onChange={(e) => setSenderName(e.target.value)} placeholder="PPBase" /></Field>
          <Field label="Sender address"><Input type="email" value={senderAddress} onChange={(e) => setSenderAddress(e.target.value)} placeholder="noreply@example.com" /></Field>
        </div>
        <Button type="submit" disabled={saving}>
          {saving && <LoadingSpinner size="sm" className="mr-2" />}Save
        </Button>
      </form>
    </SectionCard>
  )
}

// ── SMTP Tab ──────────────────────────────────────────────────────────────────
function SmtpTab({ settings, onSave, saving }: { settings: SettingsData; onSave: SaveHandler; saving: boolean }) {
  const [host, setHost] = useState('')
  const [port, setPort] = useState('587')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [tls, setTls] = useState(true)
  const [testEmail, setTestEmail] = useState('')
  const [testTemplate, setTestTemplate] = useState<TestEmailTemplate>('verification')
  const [isTesting, setIsTesting] = useState(false)

  useEffect(() => {
    const s = settings.smtp ?? {}
    setHost(String(s.host ?? ''))
    setPort(String(s.port ?? 587))
    setUsername(String(s.username ?? ''))
    setPassword(String(s.password ?? ''))
    setTls(s.tls !== false)
  }, [settings])

  useEffect(() => {
    const meta = settings.meta ?? {}
    const preferred = String(meta.supportEmail ?? meta.senderAddress ?? '').trim()
    if (!preferred) return
    setTestEmail((prev) => prev || preferred)
  }, [settings])

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    void onSave({ smtp: { host, port: Number(port), username, password, tls } })
  }

  const handleSendTestEmail = async () => {
    const recipient = testEmail.trim()
    if (!recipient) {
      toast.error('Please provide a recipient email.')
      return
    }

    setIsTesting(true)
    try {
      await onSave({ smtp: { host, port: Number(port), username, password, tls } })
      await testSettingsEmail(recipient, testTemplate)
      toast.success(`Test email sent to ${recipient}.`)
    } catch (error: unknown) {
      toast.error(getErrorMessage(error, 'Failed to send test email.'))
    } finally {
      setIsTesting(false)
    }
  }

  return (
    <SectionCard title="SMTP" description="Configure outgoing email settings.">
      <form onSubmit={handleSubmit} className="space-y-4">
        <div className="grid grid-cols-3 gap-3">
          <div className="col-span-2">
            <Field label="SMTP host"><Input value={host} onChange={(e) => setHost(e.target.value)} placeholder="smtp.example.com" /></Field>
          </div>
          <Field label="Port"><Input type="number" value={port} onChange={(e) => setPort(e.target.value)} placeholder="587" /></Field>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Username"><Input value={username} onChange={(e) => setUsername(e.target.value)} placeholder="user@example.com" /></Field>
          <Field label="Password"><Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="••••••••" /></Field>
        </div>
        <div className="flex items-center gap-2">
          <Checkbox id="smtp-tls" checked={tls} onCheckedChange={(v) => setTls(!!v)} />
          <Label htmlFor="smtp-tls" className="cursor-pointer text-sm">Enable TLS</Label>
        </div>
        <Button type="submit" disabled={saving || isTesting}>
          {saving && <LoadingSpinner size="sm" className="mr-2" />}Save
        </Button>
      </form>

      <div className="rounded-lg border border-indigo-100 bg-indigo-50/40 p-4 space-y-3">
        <h3 className="text-sm font-semibold text-indigo-900">Send test email</h3>
        <p className="text-xs text-indigo-800/80">
          Saves the SMTP values above, then sends a test message.
        </p>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <div className="md:col-span-2">
            <Field label="Recipient">
              <Input
                type="email"
                value={testEmail}
                onChange={(e) => setTestEmail(e.target.value)}
                placeholder="you@example.com"
              />
            </Field>
          </div>
          <Field label="Template">
            <Select
              value={testTemplate}
              onValueChange={(value) => setTestTemplate(value as TestEmailTemplate)}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="verification">verification</SelectItem>
                <SelectItem value="password-reset">password-reset</SelectItem>
                <SelectItem value="email-change">email-change</SelectItem>
              </SelectContent>
            </Select>
          </Field>
        </div>
        <Button
          type="button"
          variant="secondary"
          onClick={handleSendTestEmail}
          disabled={saving || isTesting}
        >
          {isTesting && <LoadingSpinner size="sm" className="mr-2" />}
          Send test email
        </Button>
      </div>
    </SectionCard>
  )
}

// ── S3 Tab ────────────────────────────────────────────────────────────────────
function S3Tab({ settings, onSave, saving }: { settings: SettingsData; onSave: SaveHandler; saving: boolean }) {
  const [endpoint, setEndpoint] = useState('')
  const [bucket, setBucket] = useState('')
  const [region, setRegion] = useState('')
  const [accessKey, setAccessKey] = useState('')
  const [secret, setSecret] = useState('')
  const [forcePathStyle, setForcePathStyle] = useState(false)

  useEffect(() => {
    const s = settings.s3 ?? {}
    setEndpoint(String(s.endpoint ?? ''))
    setBucket(String(s.bucket ?? ''))
    setRegion(String(s.region ?? ''))
    setAccessKey(String(s.accessKey ?? ''))
    setSecret(String(s.secret ?? ''))
    setForcePathStyle(!!s.forcePathStyle)
  }, [settings])

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    void onSave({ s3: { endpoint, bucket, region, accessKey, secret, forcePathStyle } })
  }

  return (
    <SectionCard title="S3 Storage" description="Configure S3-compatible object storage for file uploads.">
      <form onSubmit={handleSubmit} className="space-y-4">
        <div className="grid grid-cols-2 gap-3">
          <Field label="Endpoint"><Input value={endpoint} onChange={(e) => setEndpoint(e.target.value)} placeholder="https://s3.amazonaws.com" /></Field>
          <Field label="Bucket"><Input value={bucket} onChange={(e) => setBucket(e.target.value)} placeholder="my-bucket" /></Field>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Region"><Input value={region} onChange={(e) => setRegion(e.target.value)} placeholder="us-east-1" /></Field>
          <Field label="Access key"><Input value={accessKey} onChange={(e) => setAccessKey(e.target.value)} placeholder="AKIAIOSFODNN7EXAMPLE" /></Field>
        </div>
        <Field label="Secret key"><Input type="password" value={secret} onChange={(e) => setSecret(e.target.value)} placeholder="••••••••••••••••••••" /></Field>
        <div className="flex items-center gap-2">
          <Checkbox id="s3-path-style" checked={forcePathStyle} onCheckedChange={(v) => setForcePathStyle(!!v)} />
          <Label htmlFor="s3-path-style" className="cursor-pointer text-sm">Force path-style (for MinIO / local S3)</Label>
        </div>
        <Button type="submit" disabled={saving}>
          {saving && <LoadingSpinner size="sm" className="mr-2" />}Save
        </Button>
      </form>
    </SectionCard>
  )
}

// ── Logs Tab ──────────────────────────────────────────────────────────────────
function LogsTab({ settings, onSave, saving }: { settings: SettingsData; onSave: SaveHandler; saving: boolean }) {
  const [maxDays, setMaxDays] = useState('5')
  const [logIp, setLogIp] = useState(true)

  useEffect(() => {
    const l = settings.logs ?? {}
    setMaxDays(String(l.maxDays ?? 5))
    setLogIp(l.logIp !== false)
  }, [settings])

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    void onSave({ logs: { maxDays: Number(maxDays), logIp } })
  }

  return (
    <SectionCard title="Logs" description="Configure request log retention and privacy settings.">
      <form onSubmit={handleSubmit} className="space-y-4">
        <Field label={`Max log retention (days): ${maxDays}`}>
          <input
            type="range"
            min={1}
            max={90}
            value={maxDays}
            onChange={(e) => setMaxDays(e.target.value)}
            className="w-full accent-indigo-600"
          />
          <p className="text-xs text-muted-foreground">Logs older than {maxDays} day(s) will be automatically deleted.</p>
        </Field>
        <div className="flex items-center gap-2">
          <Checkbox id="log-ip" checked={logIp} onCheckedChange={(v) => setLogIp(!!v)} />
          <Label htmlFor="log-ip" className="cursor-pointer text-sm">Log client IP addresses</Label>
        </div>
        <Button type="submit" disabled={saving}>
          {saving && <LoadingSpinner size="sm" className="mr-2" />}Save
        </Button>
      </form>
    </SectionCard>
  )
}

// ── Rate Limiting Tab ─────────────────────────────────────────────────────────
function RateLimitTab({ settings, onSave, saving }: { settings: SettingsData; onSave: SaveHandler; saving: boolean }) {
  const [enabled, setEnabled] = useState(false)
  const [maxRequests, setMaxRequests] = useState('1000')
  const [window, setWindow] = useState('60')

  useEffect(() => {
    const r = settings.rateLimiting ?? {}
    setEnabled(!!r.enabled)
    setMaxRequests(String(r.maxRequests ?? 1000))
    setWindow(String(r.window ?? 60))
  }, [settings])

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    void onSave({ rateLimiting: { enabled, maxRequests: Number(maxRequests), window: Number(window) } })
  }

  return (
    <SectionCard title="Rate Limiting" description="Protect your API from abuse with request rate limits.">
      <form onSubmit={handleSubmit} className="space-y-4">
        <div className="flex items-center gap-2">
          <Checkbox id="rate-enabled" checked={enabled} onCheckedChange={(v) => setEnabled(!!v)} />
          <Label htmlFor="rate-enabled" className="cursor-pointer text-sm">Enable rate limiting</Label>
        </div>
        {enabled && (
          <div className="grid grid-cols-2 gap-3">
            <Field label="Max requests">
              <Input type="number" value={maxRequests} onChange={(e) => setMaxRequests(e.target.value)} placeholder="1000" />
            </Field>
            <Field label="Window (seconds)">
              <Input type="number" value={window} onChange={(e) => setWindow(e.target.value)} placeholder="60" />
            </Field>
          </div>
        )}
        <Button type="submit" disabled={saving}>
          {saving && <LoadingSpinner size="sm" className="mr-2" />}Save
        </Button>
      </form>
    </SectionCard>
  )
}

// ── Main ──────────────────────────────────────────────────────────────────────
export function SettingsForm() {
  const { data: settings = {} } = useSettings()
  const updateSettings = useUpdateSettings()

  const handleSave = async (data: SettingsData) => {
    try {
      await updateSettings.mutateAsync(data)
      toast.success('Settings saved successfully.')
    } catch (err: unknown) {
      const message = getErrorMessage(err, 'Failed to save settings.')
      toast.error(message)
      throw err
    }
  }

  const s = settings as SettingsData
  const saving = updateSettings.isPending

  return (
    <Tabs defaultValue="meta" className="space-y-4">
      <TabsList>
        <TabsTrigger value="meta">Meta</TabsTrigger>
        <TabsTrigger value="smtp">SMTP</TabsTrigger>
        <TabsTrigger value="s3">S3</TabsTrigger>
        <TabsTrigger value="logs">Logs</TabsTrigger>
        <TabsTrigger value="rate-limiting">Rate Limiting</TabsTrigger>
      </TabsList>

      <TabsContent value="meta"><MetaTab settings={s} onSave={handleSave} saving={saving} /></TabsContent>
      <TabsContent value="smtp"><SmtpTab settings={s} onSave={handleSave} saving={saving} /></TabsContent>
      <TabsContent value="s3"><S3Tab settings={s} onSave={handleSave} saving={saving} /></TabsContent>
      <TabsContent value="logs"><LogsTab settings={s} onSave={handleSave} saving={saving} /></TabsContent>
      <TabsContent value="rate-limiting"><RateLimitTab settings={s} onSave={handleSave} saving={saving} /></TabsContent>
    </Tabs>
  )
}
