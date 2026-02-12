import { useNavigate, useLocation } from 'react-router-dom'
import { useSidebar } from '@/context/sidebar-context'
import { useAuth } from '@/context/auth-context'
import { cn } from '@/lib/utils'
import { LayoutGrid, GitBranch, Settings, LogOut } from 'lucide-react'
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip'

export function SidebarIconStrip() {
  const navigate = useNavigate()
  const location = useLocation()
  const { setActiveSection, setSidebarOpen } = useSidebar()
  const { logout } = useAuth()

  const isActive = (section: string) => {
    if (section === 'collections') return location.pathname.startsWith('/collections')
    if (section === 'migrations') return location.pathname === '/migrations'
    if (section === 'settings') return location.pathname === '/settings'
    return false
  }

  const handleNav = (section: 'collections' | 'migrations' | 'settings', path: string) => {
    setActiveSection(section)
    setSidebarOpen(false)
    navigate(path)
  }

  const btnClass = 'flex items-center justify-center w-9 h-9 rounded-lg text-slate-500 hover:bg-slate-200 hover:text-slate-700 transition-colors'
  const activeClass = 'bg-indigo-100 text-indigo-600'

  return (
    <TooltipProvider>
      <div className="flex flex-col items-center w-[60px] bg-slate-50 border-r py-3 gap-1.5">
        {/* Logo */}
        <button
          className="mb-3 cursor-pointer"
          onClick={() => handleNav('collections', '/collections')}
        >
          <svg width="38" height="38" viewBox="0 0 36 36" fill="none">
            <rect width="36" height="36" rx="8" fill="#4f46e5" />
            <path d="M10 12h8a4 4 0 010 8h-8v-8zm2 2v4h6a2 2 0 000-4h-6z" fill="#fff" />
            <path d="M10 22h8a4 4 0 010 8h-8v-8zm2 2v4h6a2 2 0 000-4h-6z" fill="#fff" opacity="0.6" />
          </svg>
        </button>

        {/* Collections */}
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              className={cn(btnClass, isActive('collections') && activeClass)}
              onClick={() => handleNav('collections', '/collections')}
            >
              <LayoutGrid className="h-5 w-5" />
            </button>
          </TooltipTrigger>
          <TooltipContent side="right">
            <p>Collections</p>
          </TooltipContent>
        </Tooltip>

        {/* Migrations */}
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              className={cn(btnClass, isActive('migrations') && activeClass)}
              onClick={() => handleNav('migrations', '/migrations')}
            >
              <GitBranch className="h-5 w-5" />
            </button>
          </TooltipTrigger>
          <TooltipContent side="right">
            <p>Migrations</p>
          </TooltipContent>
        </Tooltip>

        {/* Settings */}
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              className={cn(btnClass, isActive('settings') && activeClass)}
              onClick={() => handleNav('settings', '/settings')}
            >
              <Settings className="h-5 w-5" />
            </button>
          </TooltipTrigger>
          <TooltipContent side="right">
            <p>Settings</p>
          </TooltipContent>
        </Tooltip>

        <div className="flex-1" />

        {/* Logout */}
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              className="flex items-center justify-center w-9 h-9 rounded-lg text-slate-400 hover:bg-red-50 hover:text-red-500 transition-colors"
              onClick={logout}
            >
              <LogOut className="h-5 w-5" />
            </button>
          </TooltipTrigger>
          <TooltipContent side="right">
            <p>Log out</p>
          </TooltipContent>
        </Tooltip>
      </div>
    </TooltipProvider>
  )
}

