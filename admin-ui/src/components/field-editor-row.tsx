import { useState } from 'react'
import { Field, Collection } from '@/api/types'
import { FIELD_TYPE_CONFIG } from '@/lib/field-types'
import { FieldTypeBadge } from '@/components/field-type-badge'
import { FieldOptionsPanel } from '@/components/field-options-panel'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Settings, X } from 'lucide-react'

interface FieldEditorRowProps {
  field: Field
  onChange: (field: Field) => void
  onRemove: () => void
  collections: Collection[]
}

export function FieldEditorRow({ field, onChange, onRemove, collections }: FieldEditorRowProps) {
  const [expanded, setExpanded] = useState(!field.name)
  const config = FIELD_TYPE_CONFIG[field.type]

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
    <div className="border rounded-lg mb-2 bg-background">
      <div className="flex items-center gap-2 px-3 py-2">
        <FieldTypeBadge type={field.type} />
        <div className="flex-1 min-w-0">
          <Input
            type="text"
            placeholder="Field name"
            value={field.name}
            onChange={(e) => onChange({ ...field, name: e.target.value })}
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
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className={`h-7 w-7 p-0 ${expanded ? 'text-primary' : 'text-muted-foreground'}`}
          onClick={() => setExpanded(!expanded)}
        >
          <Settings className="h-3.5 w-3.5" />
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-7 w-7 p-0 text-muted-foreground hover:text-destructive"
          onClick={onRemove}
        >
          <X className="h-3.5 w-3.5" />
        </Button>
      </div>

      {expanded && (
        <div className="px-3 pb-3">
          <FieldOptionsPanel
            field={field}
            onChange={onChange}
            collections={collections}
          />
        </div>
      )}
    </div>
  )
}
