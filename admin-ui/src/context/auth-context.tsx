import React, { createContext, useContext, useState, useEffect, useCallback } from 'react'
import { apiClient } from '@/api/client'
import { login as loginApi } from '@/api/endpoints/auth'

interface AuthContextType {
  token: string | null
  isAuthenticated: boolean
  login: (email: string, password: string) => Promise<void>
  logout: () => void
  isLoading: boolean
}

const AuthContext = createContext<AuthContextType | null>(null)

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [token, setToken] = useState<string | null>(apiClient.getToken())
  const [isLoading, setIsLoading] = useState(true)

  useEffect(() => {
    // Validate existing token on mount
    const existingToken = apiClient.getToken()
    if (existingToken) {
      apiClient.request('GET', '/api/collections?perPage=1')
        .then(() => {
          setToken(existingToken)
          setIsLoading(false)
        })
        .catch(() => {
          apiClient.clearToken()
          setToken(null)
          setIsLoading(false)
        })
    } else {
      setIsLoading(false)
    }
  }, [])

  const login = useCallback(async (email: string, password: string) => {
    const result = await loginApi(email, password)
    apiClient.setToken(result.token)
    setToken(result.token)
  }, [])

  const logout = useCallback(() => {
    apiClient.clearToken()
    setToken(null)
  }, [])

  return (
    <AuthContext.Provider value={{ token, isAuthenticated: !!token, login, logout, isLoading }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used within AuthProvider')
  return context
}
