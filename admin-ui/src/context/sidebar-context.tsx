import React, { createContext, useContext, useState } from 'react'

interface SidebarContextType {
  activeSection: 'collections' | 'migrations' | 'settings'
  selectedCollectionId: string | null
  setActiveSection: (section: 'collections' | 'migrations' | 'settings') => void
  setSelectedCollectionId: (id: string | null) => void
}

const SidebarContext = createContext<SidebarContextType | null>(null)

export function SidebarProvider({ children }: { children: React.ReactNode }) {
  const [activeSection, setActiveSection] = useState<'collections' | 'migrations' | 'settings'>('collections')
  const [selectedCollectionId, setSelectedCollectionId] = useState<string | null>(null)

  return (
    <SidebarContext.Provider value={{ activeSection, selectedCollectionId, setActiveSection, setSelectedCollectionId }}>
      {children}
    </SidebarContext.Provider>
  )
}

export function useSidebar() {
  const context = useContext(SidebarContext)
  if (!context) throw new Error('useSidebar must be used within SidebarProvider')
  return context
}
