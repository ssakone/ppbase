import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '@/context/auth-context'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { LoadingSpinner } from '@/components/loading-spinner'

export function LoginPage() {
  const { login, isAuthenticated, needsSetup } = useAuth()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [isLoading, setIsLoading] = useState(false)

  if (isAuthenticated) {
    navigate('/collections', { replace: true })
    return null
  }

  if (needsSetup) {
    navigate('/setup', { replace: true })
    return null
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!email.trim() || !password) return

    setIsLoading(true)
    setError('')

    try {
      await login(email.trim(), password)
      navigate('/collections', { replace: true })
    } catch (err: unknown) {
      const message = (err as { message?: string })?.message || 'Invalid email or password.'
      setError(message)
      setPassword('')
    } finally {
      setIsLoading(false)
    }
  }

  return (
    <div className="flex items-center justify-center min-h-screen bg-gradient-to-br from-slate-100 via-slate-50 to-indigo-50/30">
      <div className="w-full max-w-sm animate-fade-in">
        {/* Card */}
        <div className="bg-white rounded-2xl shadow-lg border border-slate-200/80 px-8 py-10">
          {/* Logo */}
          <div className="flex flex-col items-center gap-3 mb-8">
            <div className="p-1 rounded-2xl bg-indigo-50">
              <svg width="48" height="48" viewBox="0 0 36 36" fill="none">
                <rect width="36" height="36" rx="10" fill="#4f46e5"/>
                <path d="M10 12h8a4 4 0 010 8h-8v-8zm2 2v4h6a2 2 0 000-4h-6z" fill="#fff"/>
                <path d="M10 22h8a4 4 0 010 8h-8v-8zm2 2v4h6a2 2 0 000-4h-6z" fill="#fff" opacity="0.6"/>
              </svg>
            </div>
            <div className="text-center">
              <h1 className="text-2xl font-bold text-foreground tracking-tight">PPBase</h1>
              <p className="text-sm text-muted-foreground mt-1">Sign in to your admin dashboard</p>
            </div>
          </div>

          <form onSubmit={handleSubmit} className="space-y-5">
            <div className="space-y-2">
              <Label htmlFor="login-email" className="text-sm font-medium">Email</Label>
              <Input
                id="login-email"
                type="email"
                placeholder="admin@example.com"
                required
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="login-password" className="text-sm font-medium">Password</Label>
              <Input
                id="login-password"
                type="password"
                placeholder="Enter your password"
                required
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>

            {error && (
              <div className="text-sm text-destructive bg-destructive/10 rounded-lg px-4 py-2.5 border border-destructive/20">
                {error}
              </div>
            )}

            <Button type="submit" className="w-full h-11 text-sm font-medium" disabled={isLoading}>
              {isLoading ? <LoadingSpinner size="sm" /> : 'Sign in'}
            </Button>
          </form>
        </div>
      </div>
    </div>
  )
}
