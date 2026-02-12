import React, { createContext, useContext, useState, useCallback } from 'react'

const COLLECTIONS_PANEL_WIDTH_KEY = 'ppbase_collections_panel_width'
const DEFAULT_WIDTH = 300
const MIN_WIDTH = 200
const MAX_WIDTH = 500

interface SidebarContextType {
  activeSection: 'collections' | 'migrations' | 'settings'
  selectedCollectionId: string | null
  sidebarOpen: boolean
  collectionsPanelWidth: number
  setActiveSection: (section: 'collections' | 'migrations' | 'settings') => void
  setSelectedCollectionId: (id: string | null) => void
  setSidebarOpen: (open: boolean) => void
  setCollectionsPanelWidth: (width: number) => void
}

const SidebarContext = createContext<SidebarContextType | null>(null)

function getStoredWidth(): number {
  if (typeof window === 'undefined') return DEFAULT_WIDTH
  const stored = localStorage.getItem(COLLECTIONS_PANEL_WIDTH_KEY)
  if (!stored) return DEFAULT_WIDTH
  const n = parseInt(stored, 10)
  return Number.isNaN(n) ? DEFAULT_WIDTH : Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, n))
}

export function SidebarProvider({ children }: { children: React.ReactNode }) {
  const [activeSection, setActiveSection] = useState<'collections' | 'migrations' | 'settings'>('collections')
  const [selectedCollectionId, setSelectedCollectionId] = useState<string | null>(null)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [collectionsPanelWidth, setCollectionsPanelWidthState] = useState(getStoredWidth)

  const setCollectionsPanelWidth = useCallback((width: number) => {
    const clamped = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, width))
    setCollectionsPanelWidthState(clamped)
    localStorage.setItem(COLLECTIONS_PANEL_WIDTH_KEY, String(clamped))
  }, [])

  return (
    <SidebarContext.Provider value={{
      activeSection,
      selectedCollectionId,
      sidebarOpen,
      collectionsPanelWidth,
      setActiveSection,
      setSelectedCollectionId,
      setSidebarOpen,
      setCollectionsPanelWidth,
    }}>
      {children}
    </SidebarContext.Provider>
  )
}

export function useSidebar() {
  const context = useContext(SidebarContext)
  if (!context) throw new Error('useSidebar must be used within SidebarProvider')
  return context
}
