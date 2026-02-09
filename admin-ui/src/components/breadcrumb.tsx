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
    <div className="flex items-center gap-1.5 text-base">
      {items.map((item, i) => (
        <span key={i} className="flex items-center gap-1.5">
          {i > 0 && (
            <span className="text-muted-foreground/50">/</span>
          )}
          <span
            className={cn(
              item.active ? 'font-semibold text-foreground' : 'text-muted-foreground cursor-pointer hover:text-foreground',
            )}
            onClick={item.onClick}
          >
            {item.label}
          </span>
        </span>
      ))}
      {children}
    </div>
  )
}
