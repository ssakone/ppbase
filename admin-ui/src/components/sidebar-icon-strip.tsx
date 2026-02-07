import { useNavigate, useLocation } from 'react-router-dom'
import { useSidebar } from '@/context/sidebar-context'
import { useAuth } from '@/context/auth-context'
import { cn } from '@/lib/utils'

export function SidebarIconStrip() {
  const navigate = useNavigate()
  const location = useLocation()
  const { setActiveSection } = useSidebar()
  const { logout } = useAuth()

  const isActive = (section: string) => {
    if (section === 'collections') return location.pathname.startsWith('/collections')
    if (section === 'migrations') return location.pathname === '/migrations'
    if (section === 'settings') return location.pathname === '/settings'
    return false
  }

  const handleNav = (section: 'collections' | 'migrations' | 'settings', path: string) => {
    setActiveSection(section)
    navigate(path)
  }

  const btnClass = 'flex items-center justify-center w-10 h-10 rounded-xl text-slate-500 hover:bg-slate-200 hover:text-slate-700 transition-colors'
  const activeClass = 'bg-indigo-100 text-indigo-600'

  return (
    <div className="flex flex-col items-center w-[75px] bg-slate-50 border-r py-4 gap-2">
      {/* Logo */}
      <div className="mb-4">
        <svg width="46" height="46" viewBox="0 0 36 36" fill="none">
          <rect width="36" height="36" rx="8" fill="#4f46e5"/>
          <path d="M10 12h8a4 4 0 010 8h-8v-8zm2 2v4h6a2 2 0 000-4h-6z" fill="#fff"/>
          <path d="M10 22h8a4 4 0 010 8h-8v-8zm2 2v4h6a2 2 0 000-4h-6z" fill="#fff" opacity="0.6"/>
        </svg>
      </div>

      {/* Collections */}
      <button
        className={cn(btnClass, isActive('collections') && activeClass)}
        onClick={() => handleNav('collections', '/collections')}
        title="Collections"
      >
        <svg width="34" height="34" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <rect x="3" y="3" width="6" height="6" rx="1"/>
          <rect x="11" y="3" width="6" height="6" rx="1"/>
          <rect x="3" y="11" width="6" height="6" rx="1"/>
          <rect x="11" y="11" width="6" height="6" rx="1"/>
        </svg>
      </button>

      {/* Migrations */}
      <button
        className={cn(btnClass, isActive('migrations') && activeClass)}
        onClick={() => handleNav('migrations', '/migrations')}
        title="Migrations"
      >
        <svg width="34" height="34" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M3 5h14M3 10h14M3 15h14"/>
          <path d="M6 3v4M14 8v4M10 13v4"/>
        </svg>
      </button>

      {/* Settings */}
      <button
        className={cn(btnClass, isActive('settings') && activeClass)}
        onClick={() => handleNav('settings', '/settings')}
        title="Settings"
      >
        <svg width="34" height="34" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="10" cy="10" r="2.5"/>
          <path d="M16.5 10a6.5 6.5 0 01-.3 1.4l1.3 1a.4.4 0 01.1.5l-1.3 2.2a.4.4 0 01-.5.1l-1.5-.6a6 6 0 01-1.2.7l-.2 1.6a.4.4 0 01-.4.2H8.5a.4.4 0 01-.4-.2l-.2-1.6a6 6 0 01-1.2-.7l-1.5.6a.4.4 0 01-.5-.1L3.4 12.9a.4.4 0 01.1-.5l1.3-1A6.5 6.5 0 014.5 10c0-.5 0-.9.1-1.4l-1.3-1a.4.4 0 01-.1-.5l1.3-2.2a.4.4 0 01.5-.1l1.5.6a6 6 0 011.2-.7l.2-1.6A.4.4 0 018.5 3h2.6a.4.4 0 01.4.2l.2 1.6a6 6 0 011.2.7l1.5-.6a.4.4 0 01.5.1l1.3 2.2a.4.4 0 01-.1.5l-1.3 1c.1.5.2.9.2 1.4z"/>
        </svg>
      </button>

      <div className="flex-1" />

      {/* Logout */}
      <button
        className="flex items-center justify-center w-10 h-10 rounded-xl text-slate-400 hover:bg-red-50 hover:text-red-500 transition-colors"
        onClick={logout}
        title="Log out"
      >
        <svg width="34" height="34" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M7.5 17.5H4.17a1.67 1.67 0 01-1.67-1.67V4.17A1.67 1.67 0 014.17 2.5H7.5M13.33 14.17L17.5 10l-4.17-4.17M17.5 10H7.5"/>
        </svg>
      </button>
    </div>
  )
}
