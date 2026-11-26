import React, { createContext, useCallback, useContext, useEffect, useState } from 'react'

// Keep in sync with the inline script in index.html (applied before first paint).
const THEME_STORAGE_KEY = 'ppbase_theme'

export type Theme = 'light' | 'dark' | 'system'
type ResolvedTheme = 'light' | 'dark'

interface ThemeContextType {
  theme: Theme
  resolvedTheme: ResolvedTheme
  setTheme: (theme: Theme) => void
}

const ThemeContext = createContext<ThemeContextType | null>(null)

const darkQuery = () => window.matchMedia('(prefers-color-scheme: dark)')

function getStoredTheme(): Theme {
  const stored = localStorage.getItem(THEME_STORAGE_KEY)
  return stored === 'light' || stored === 'dark' ? stored : 'system'
}

function resolveTheme(theme: Theme): ResolvedTheme {
  if (theme !== 'system') return theme
  return darkQuery().matches ? 'dark' : 'light'
}

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(getStoredTheme)
  const [resolvedTheme, setResolvedTheme] = useState<ResolvedTheme>(() => resolveTheme(theme))

  useEffect(() => {
    const apply = () => {
      const next = resolveTheme(theme)
      setResolvedTheme(next)
      document.documentElement.classList.toggle('dark', next === 'dark')
    }
    apply()
    if (theme !== 'system') return
    const query = darkQuery()
    query.addEventListener('change', apply)
    return () => query.removeEventListener('change', apply)
  }, [theme])

  const setTheme = useCallback((next: Theme) => {
    if (next === 'system') {
      localStorage.removeItem(THEME_STORAGE_KEY)
    } else {
      localStorage.setItem(THEME_STORAGE_KEY, next)
    }
    setThemeState(next)
  }, [])

  return (
    <ThemeContext.Provider value={{ theme, resolvedTheme, setTheme }}>
      {children}
    </ThemeContext.Provider>
  )
}

export function useTheme() {
  const ctx = useContext(ThemeContext)
  if (!ctx) throw new Error('useTheme must be used within ThemeProvider')
  return ctx
}
