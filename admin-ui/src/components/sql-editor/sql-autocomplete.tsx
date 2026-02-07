import { useEffect, useRef } from 'react'
import { cn } from '@/lib/utils'

export interface AutocompleteItem {
  label: string
  kind: 'table' | 'column' | 'keyword'
  detail?: string
}

interface SqlAutocompleteProps {
  items: AutocompleteItem[]
  activeIndex: number
  onSelect: (index: number) => void
  position: { left: number; top: number }
}

const kindConfig = {
  table: { icon: 'T', className: 'bg-blue-100 text-blue-700' },
  column: { icon: 'C', className: 'bg-green-100 text-green-700' },
  keyword: { icon: 'K', className: 'bg-purple-100 text-purple-700' },
}

export function SqlAutocomplete({ items, activeIndex, onSelect, position }: SqlAutocompleteProps) {
  const listRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const active = listRef.current?.querySelector('[data-active="true"]')
    if (active) {
      active.scrollIntoView({ block: 'nearest' })
    }
  }, [activeIndex])

  if (items.length === 0) return null

  return (
    <div
      ref={listRef}
      className="absolute z-50 bg-popover border rounded-md shadow-lg max-h-[200px] overflow-y-auto min-w-[200px]"
      style={{ left: Math.min(position.left, 280), top: position.top + 20 }}
    >
      {items.map((item, i) => {
        const cfg = kindConfig[item.kind]
        return (
          <div
            key={`${item.kind}-${item.label}-${i}`}
            data-active={i === activeIndex}
            className={cn(
              'flex items-center gap-2 px-2 py-1 text-sm cursor-pointer',
              i === activeIndex ? 'bg-accent text-accent-foreground' : 'hover:bg-muted',
            )}
            onMouseDown={(e) => {
              e.preventDefault()
              onSelect(i)
            }}
          >
            <span
              className={cn(
                'inline-flex items-center justify-center rounded text-[10px] font-bold w-5 h-5 shrink-0',
                cfg.className,
              )}
            >
              {cfg.icon}
            </span>
            <span className="truncate">{item.label}</span>
            {item.detail && (
              <span className="ml-auto text-xs text-muted-foreground truncate">
                {item.detail}
              </span>
            )}
          </div>
        )
      })}
    </div>
  )
}
