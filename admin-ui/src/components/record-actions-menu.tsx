import { Copy, CopyPlus, Trash2 } from 'lucide-react'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import type { RecordModel } from '@/api/types'

interface RecordActionsMenuProps {
  record: RecordModel
  trigger: React.ReactNode
  onDuplicate: () => void
  onDelete: () => void
}

export function RecordActionsMenu({
  record,
  trigger,
  onDuplicate,
  onDelete,
}: RecordActionsMenuProps) {
  const handleCopyJson = () => {
    navigator.clipboard.writeText(JSON.stringify(record, null, 2))
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>{trigger}</DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem onClick={handleCopyJson}>
          <Copy className="mr-2 h-4 w-4" />
          Copy raw JSON
        </DropdownMenuItem>
        <DropdownMenuItem onClick={onDuplicate}>
          <CopyPlus className="mr-2 h-4 w-4" />
          Duplicate
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          onClick={onDelete}
          className="text-destructive focus:text-destructive"
        >
          <Trash2 className="mr-2 h-4 w-4" />
          Delete
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
