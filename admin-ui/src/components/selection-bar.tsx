import { Button } from '@/components/ui/button'
import { X, Trash2 } from 'lucide-react'

interface SelectionBarProps {
  count: number
  onReset: () => void
  onDelete: () => void
}

export function SelectionBar({ count, onReset, onDelete }: SelectionBarProps) {
  if (count === 0) return null

  return (
    <div className="fixed bottom-0 left-0 right-0 z-40 flex items-center gap-3 border-t bg-white px-6 py-3 shadow-lg">
      <span className="text-sm font-medium">
        Selected {count} record{count !== 1 ? 's' : ''}
      </span>
      <Button variant="ghost" size="sm" onClick={onReset}>
        <X className="mr-1 h-3 w-3" />
        Reset
      </Button>
      <div className="flex-1" />
      <Button variant="destructive" size="sm" onClick={onDelete}>
        <Trash2 className="mr-1 h-4 w-4" />
        Delete selected
      </Button>
    </div>
  )
}
