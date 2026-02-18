import { useEffect, useState } from 'react'
import { Navigate, useNavigate, useSearchParams } from 'react-router-dom'
import { useAuth } from '@/context/auth-context'
import { createFirstAdmin, getInitStatus } from '@/api/endpoints/init'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { LoadingSpinner } from '@/components/loading-spinner'
import { navigateWithTransition } from '@/lib/navigation'

export function SetupPage() {
  const { isAuthenticated, loginWithToken } = useAuth()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const token = searchParams.get('token')

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [passwordConfirm, setPasswordConfirm] = useState('')
  const [error, setError] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const [needsSetup, setNeedsSetup] = useState<boolean | null>(null)
  const [checkingSetup, setCheckingSetup] = useState(true)

  useEffect(() => {
    if (!token) {
      setNeedsSetup(false)
      setCheckingSetup(false)
      return
    }
    getInitStatus(token)
      .then((res) => {
        setNeedsSetup(res.needsSetup)
      })
      .catch(() => setNeedsSetup(false))
      .finally(() => setCheckingSetup(false))
  }, [token])

  // If already authenticated, go to dashboard
  if (isAuthenticated) {
    return <Navigate to="/dashboard" replace />
  }

  // No token in URL — show message to use the console URL
  if (!token && !checkingSetup) {
    return (
      <div className="flex flex-col items-center justify-center min-h-screen bg-slate-50 px-4">
        <div className="w-full max-w-md p-8 bg-white rounded-xl shadow-sm border text-center">
          <div className="flex justify-center mb-4">
            <svg width="48" height="48" viewBox="0 0 36 36" fill="none">
              <rect width="36" height="36" rx="8" fill="#4f46e5"/>
              <path d="M10 12h8a4 4 0 010 8h-8v-8zm2 2v4h6a2 2 0 000-4h-6z" fill="#fff"/>
              <path d="M10 22h8a4 4 0 010 8h-8v-8zm2 2v4h6a2 2 0 000-4h-6z" fill="#fff" opacity="0.6"/>
            </svg>
          </div>
          <h1 className="text-xl font-semibold text-foreground mb-2">Create your first admin</h1>
          <p className="text-muted-foreground">
            Open the one-time URL printed in the server console when you started PPBase.
          </p>
          <p className="text-sm text-muted-foreground mt-4">
            It looks like: <code className="bg-slate-100 px-1 rounded">http://127.0.0.1:8090/_/setup?token=...</code>
          </p>
        </div>
      </div>
    )
  }

  // Token invalid or setup not needed — go to login
  if (!checkingSetup && token && !needsSetup) {
    return <Navigate to="/login" replace />
  }

  // Loading or waiting for needsSetup
  if (checkingSetup || needsSetup === null) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-slate-50">
        <LoadingSpinner size="lg" />
      </div>
    )
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!token || !email.trim() || !password || !passwordConfirm) return

    if (password !== passwordConfirm) {
      setError('Passwords do not match.')
      return
    }

    if (password.length < 8) {
      setError('Password must be at least 8 characters.')
      return
    }

    setIsLoading(true)
    setError('')

    try {
      const result = await createFirstAdmin(email.trim(), password, passwordConfirm, token)
      loginWithToken(result.token)
      navigateWithTransition(navigate, '/dashboard', { replace: true })
    } catch (err: unknown) {
      const message = (err as { message?: string })?.message || 'Failed to create admin account.'
      setError(message)
    } finally {
      setIsLoading(false)
    }
  }

  return (
    <div className="flex items-center justify-center min-h-screen bg-slate-50">
      <div className="w-full max-w-sm p-8 bg-white rounded-xl shadow-sm border">
        <div className="flex items-center gap-3 mb-2 justify-center">
          <svg width="40" height="40" viewBox="0 0 36 36" fill="none">
            <rect width="36" height="36" rx="8" fill="#4f46e5"/>
            <path d="M10 12h8a4 4 0 010 8h-8v-8zm2 2v4h6a2 2 0 000-4h-6z" fill="#fff"/>
            <path d="M10 22h8a4 4 0 010 8h-8v-8zm2 2v4h6a2 2 0 000-4h-6z" fill="#fff" opacity="0.6"/>
          </svg>
          <h1 className="text-2xl font-bold text-foreground">PPBase</h1>
        </div>
        <p className="text-sm text-muted-foreground text-center mb-6">
          Create your first admin account
        </p>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="setup-email">Email</Label>
            <Input
              id="setup-email"
              type="email"
              placeholder="admin@example.com"
              required
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="setup-password">Password</Label>
            <Input
              id="setup-password"
              type="password"
              placeholder="Min. 8 characters"
              required
              autoComplete="new-password"
              minLength={8}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="setup-password-confirm">Confirm password</Label>
            <Input
              id="setup-password-confirm"
              type="password"
              placeholder="Repeat your password"
              required
              autoComplete="new-password"
              minLength={8}
              value={passwordConfirm}
              onChange={(e) => setPasswordConfirm(e.target.value)}
            />
          </div>

          {error && (
            <div className="text-sm text-destructive bg-destructive/10 rounded-md px-3 py-2">
              {error}
            </div>
          )}

          <Button type="submit" className="w-full" disabled={isLoading}>
            {isLoading && <LoadingSpinner size="sm" className="mr-2" />}
            Create admin
          </Button>
        </form>
      </div>
    </div>
  )
}
