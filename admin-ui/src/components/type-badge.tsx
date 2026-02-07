import { cn } from '@/lib/utils'

interface TypeBadgeProps {
  type: 'base' | 'auth' | 'view' | string
  className?: string
}

export function TypeBadge({ type, className }: TypeBadgeProps) {
  const styles: Record<string, string> = {
    base: 'bg-blue-100 text-blue-700',
    auth: 'bg-green-100 text-green-700',
    view: 'bg-amber-100 text-amber-700',
  }

  return (
    <span
      className={cn(
        'inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium',
        styles[type] || 'bg-slate-100 text-slate-600',
        className,
      )}
    >
      {type}
    </span>
  )
}
