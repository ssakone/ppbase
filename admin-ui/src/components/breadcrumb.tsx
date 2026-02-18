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
    <div className="flex items-center gap-2 text-[15px] md:text-[16px] min-w-0 flex-wrap">
      {items.map((item, i) => (
        <span key={i} className="flex items-center gap-2">
          {i > 0 && (
            <span className="text-muted-foreground/40 font-light select-none">/</span>
          )}
          <span
            className={cn(
              'truncate max-w-[200px] sm:max-w-none transition-colors',
              item.active
                ? 'font-semibold text-foreground'
                : 'text-muted-foreground cursor-pointer hover:text-foreground',
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
