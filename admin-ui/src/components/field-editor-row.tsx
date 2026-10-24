import { useState } from 'react'
import { Field, Collection } from '@/api/types'
import { FieldTypeBadge } from '@/components/field-type-badge'
import { FieldOptionsPanel } from '@/components/field-options-panel'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  canDuplicateCollectionField,
  canRemoveCollectionField,
} from '@/lib/collection-field-behavior'
import { CopyPlus, MoreHorizontal, Settings, Trash2 } from 'lucide-react'

interface FieldEditorRowProps {
  field: Field
  onChange: (field: Field) => void
  onDuplicate: () => void
  onRemove: () => void
  collections: Collection[]
  collectionType: 'base' | 'auth' | 'view'
}

export function FieldEditorRow({
  field,
  onChange,
  onDuplicate,
  onRemove,
  collections,
  collectionType,
}: FieldEditorRowProps) {
  const [expanded, setExpanded] = useState(!field.name)
  const canDuplicate = canDuplicateCollectionField(field)
  const canRemove = canRemoveCollectionField(field)

  const getInfoText = (): string => {
    const opts = field.options || {}
    switch (field.type) {
      case 'relation': {
        const cid = field.collectionId || opts.collectionId || ''
        if (cid) {
          const rel = collections.find((c) => c.id === cid)
          if (rel) return rel.name
        }
        return cid || ''
      }
      case 'select': {
        const vals = field.values || opts.values || []
        if (vals.length === 0) return ''
        return vals.slice(0, 3).join(', ') + (vals.length > 3 ? '...' : '')
      }
      default:
        return ''
    }
  }

  const infoText = getInfoText()

  return (
    <div className="border rounded-lg mb-2 bg-background" data-field-name={field.name}>
      <div className="flex items-center gap-2 px-3 py-2">
        <FieldTypeBadge type={field.type} />
        <div className="flex-1 min-w-0">
          <Input
            type="text"
            placeholder="Field name"
            value={field.name}
            onChange={(e) => onChange({ ...field, name: e.target.value })}
            disabled={field.system}
            className="h-7 text-sm border-0 shadow-none px-1 focus-visible:ring-0"
          />
        </div>
        {infoText && (
          <span className="text-xs text-muted-foreground truncate max-w-[120px]">
            {infoText}
          </span>
        )}
        {field.required && (
          <Badge
            variant="outline"
            className="bg-green-100 text-green-700 border-green-200 text-[10px] px-1.5 py-0"
          >
            Required
          </Badge>
        )}
        {field.hidden && (
          <Badge
            variant="outline"
            className="bg-red-100 text-red-700 border-red-200 text-[10px] px-1.5 py-0"
          >
            Hidden
          </Badge>
        )}
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className={`h-7 w-7 p-0 ${expanded ? 'text-primary' : 'text-muted-foreground'}`}
          onClick={() => setExpanded(!expanded)}
          aria-label={`Field options for ${field.name}`}
          data-field-action="settings"
        >
          <Settings className="h-3.5 w-3.5" />
        </Button>
        {(canDuplicate || canRemove) && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-7 w-7 p-0 text-muted-foreground"
                aria-label={`More options for ${field.name}`}
                data-field-action="menu"
              >
                <MoreHorizontal className="h-3.5 w-3.5" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              {canDuplicate && (
                <DropdownMenuItem onClick={onDuplicate}>
                  <CopyPlus className="mr-2 h-4 w-4" />
                  Duplicate
                </DropdownMenuItem>
              )}
              {canDuplicate && canRemove && <DropdownMenuSeparator />}
              {canRemove && (
                <DropdownMenuItem
                  className="text-destructive focus:text-destructive"
                  onClick={onRemove}
                >
                  <Trash2 className="mr-2 h-4 w-4" />
                  Remove
                </DropdownMenuItem>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>

      {expanded && (
        <div className="px-3 pb-3">
          <FieldOptionsPanel
            field={field}
            onChange={onChange}
            collections={collections}
            collectionType={collectionType}
          />
        </div>
      )}
    </div>
  )
}
