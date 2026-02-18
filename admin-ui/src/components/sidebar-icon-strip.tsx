import { useNavigate, useLocation } from 'react-router-dom'
import { useSidebar } from '@/context/sidebar-context'
import { useAuth } from '@/context/auth-context'
import { cn } from '@/lib/utils'
import { LayoutGrid, GitBranch, Settings, LogOut, Activity, Users2 } from 'lucide-react'
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
    if (section === 'logs') return location.pathname === '/logs'
    if (section === 'settings') return location.pathname === '/settings'
    return false
  }

  const handleNav = (path: string, section?: 'collections' | 'migrations' | 'settings') => {
    if (section) setActiveSection(section)
    setSidebarOpen(false)
    navigate(path)
  }

  const btnClass =
    'flex items-center justify-center w-11 h-11 rounded-xl text-slate-500 hover:bg-slate-200/80 hover:text-slate-700 transition-all duration-150'
  const activeClass = 'bg-indigo-100 text-indigo-600 shadow-sm'

  return (
    <TooltipProvider delayDuration={300}>
      <div className="flex flex-col items-center w-[72px] bg-slate-50 border-r py-4 gap-1">
        {/* Logo */}
        <button
          className="mb-4 cursor-pointer hover:scale-105 transition-transform duration-150"
          onClick={() => handleNav('/collections', 'collections')}
        >
          <svg width="42" height="42" viewBox="0 0 36 36" fill="none">
            <rect width="36" height="36" rx="10" fill="#4f46e5" />
            <path d="M10 12h8a4 4 0 010 8h-8v-8zm2 2v4h6a2 2 0 000-4h-6z" fill="#fff" />
            <path d="M10 22h8a4 4 0 010 8h-8v-8zm2 2v4h6a2 2 0 000-4h-6z" fill="#fff" opacity="0.6" />
          </svg>
        </button>

        {/* Collections */}
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              className={cn(btnClass, isActive('collections') && activeClass)}
              onClick={() => handleNav('/collections', 'collections')}
            >
              <LayoutGrid className="h-5 w-5" />
            </button>
          </TooltipTrigger>
          <TooltipContent side="right" className="text-sm">Collections</TooltipContent>
        </Tooltip>

        {/* Superusers */}
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              className={cn(btnClass, location.pathname === '/collections/_superusers' && activeClass)}
              onClick={() => handleNav('/collections/_superusers', 'collections')}
            >
              <Users2 className="h-5 w-5" />
            </button>
          </TooltipTrigger>
          <TooltipContent side="right" className="text-sm">Superusers</TooltipContent>
        </Tooltip>

        {/* Migrations */}
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              className={cn(btnClass, isActive('migrations') && activeClass)}
              onClick={() => handleNav('/migrations', 'migrations')}
            >
              <GitBranch className="h-5 w-5" />
            </button>
          </TooltipTrigger>
          <TooltipContent side="right" className="text-sm">Migrations</TooltipContent>
        </Tooltip>

        {/* Logs */}
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              className={cn(btnClass, isActive('logs') && activeClass)}
              onClick={() => handleNav('/logs')}
            >
              <Activity className="h-5 w-5" />
            </button>
          </TooltipTrigger>
          <TooltipContent side="right" className="text-sm">Logs</TooltipContent>
        </Tooltip>

        {/* Settings */}
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              className={cn(btnClass, isActive('settings') && activeClass)}
              onClick={() => handleNav('/settings', 'settings')}
            >
              <Settings className="h-5 w-5" />
            </button>
          </TooltipTrigger>
          <TooltipContent side="right" className="text-sm">Settings</TooltipContent>
        </Tooltip>

        <div className="flex-1" />

        {/* Logout */}
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              className="flex items-center justify-center w-11 h-11 rounded-xl text-slate-400 hover:bg-red-50 hover:text-red-500 transition-all duration-150"
              onClick={logout}
            >
              <LogOut className="h-5 w-5" />
            </button>
          </TooltipTrigger>
          <TooltipContent side="right" className="text-sm">Log out</TooltipContent>
        </Tooltip>
      </div>
    </TooltipProvider>
  )
}
