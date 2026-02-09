import { SidebarIconStrip } from './sidebar-icon-strip'
import { SidebarCollectionsPanel } from './sidebar-collections-panel'

export function AppSidebar() {
  return (
    <aside className="flex h-screen shrink-0">
      <SidebarIconStrip />
      <SidebarCollectionsPanel />
    </aside>
  )
}
