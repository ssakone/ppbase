import React, { createContext, useContext, useState, useEffect, useCallback } from 'react'
import { apiClient } from '@/api/client'
import { login as loginApi } from '@/api/endpoints/auth'
import { getInitStatus } from '@/api/endpoints/init'

interface AuthContextType {
  token: string | null
  isAuthenticated: boolean
  needsSetup: boolean
  login: (email: string, password: string) => Promise<void>
  loginWithToken: (token: string) => void
  logout: () => void
  setNeedsSetup: (v: boolean) => void
  isLoading: boolean
}

const AuthContext = createContext<AuthContextType | null>(null)

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [token, setToken] = useState<string | null>(apiClient.getToken())
  const [isLoading, setIsLoading] = useState(true)
  const [needsSetup, setNeedsSetup] = useState(false)

  useEffect(() => {
    const existingToken = apiClient.getToken()
    if (existingToken) {
      // Validate existing token
      apiClient.request('GET', '/api/collections?perPage=1')
        .then(() => {
          setToken(existingToken)
          setIsLoading(false)
        })
        .catch(() => {
          apiClient.clearToken()
          setToken(null)
          // Token invalid — check if setup is needed
          getInitStatus()
            .then((res) => setNeedsSetup(res.needsSetup))
            .catch(() => {})
            .finally(() => setIsLoading(false))
        })
    } else {
      // No token — check if setup is needed
      getInitStatus()
        .then((res) => setNeedsSetup(res.needsSetup))
        .catch(() => {})
        .finally(() => setIsLoading(false))
    }
  }, [])

  const login = useCallback(async (email: string, password: string) => {
    const result = await loginApi(email, password)
    apiClient.setToken(result.token)
    setToken(result.token)
    setNeedsSetup(false)
  }, [])

  const loginWithToken = useCallback((newToken: string) => {
    apiClient.setToken(newToken)
    setToken(newToken)
    setNeedsSetup(false)
  }, [])

  const logout = useCallback(() => {
    apiClient.clearToken()
    setToken(null)
  }, [])

  return (
    <AuthContext.Provider value={{
      token,
      isAuthenticated: !!token,
      needsSetup,
      login,
      loginWithToken,
      logout,
      setNeedsSetup,
      isLoading,
    }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used within AuthProvider')
  return context
}
