import { cn } from '@/lib/utils'

interface BreadcrumbItem {
  label: string
  onClick?: () => void
  active?: boolean
}

interface BreadcrumbProps {
  items: BreadcrumbItem[]
  children?: React.ReactNode
}

export function Breadcrumb({ items, children }: BreadcrumbProps) {
  return (
    <div className="flex items-center gap-2 text-sm md:text-base min-w-0 flex-wrap">
      {items.map((item, i) => (
        <span key={i} className="flex items-center gap-2">
          {i > 0 && (
            <span className="text-muted-foreground/40 font-light">/</span>
          )}
          <span
            className={cn(
              'truncate max-w-[160px] sm:max-w-none',
              item.active ? 'font-semibold text-foreground' : 'text-muted-foreground cursor-pointer hover:text-foreground',
            )}
            onClick={item.onClick}
            title={item.label}
          >
            {item.label}
          </span>
        </span>
      ))}
      {children}
    </div>
  )
}
