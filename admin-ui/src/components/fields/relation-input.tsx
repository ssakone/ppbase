import { useMemo, useState } from 'react'
import { ExternalLink } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { RelationPickerDialog } from '@/components/fields/relation-picker-dialog'
import type { FieldInputProps } from '@/components/record-field-input'
import { getRelationDisplayValue } from '@/lib/format-cell'
import { truncateId } from '@/lib/utils'
import type { RecordModel } from '@/api/types'

function parseIds(value: unknown, isMulti: boolean): string[] {
  if (Array.isArray(value)) return value.map((v) => String(v).trim()).filter(Boolean)
  if (!value) return []
  const str = String(value).trim()
  if (!isMulti) return str ? [str] : []
  return str
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
}

function toExpandedRecords(rawExpand: unknown): RecordModel[] {
  if (Array.isArray(rawExpand)) {
    return rawExpand.filter((item): item is RecordModel => !!item && typeof item === 'object' && 'id' in item)
  }
  if (rawExpand && typeof rawExpand === 'object' && 'id' in rawExpand) {
    return [rawExpand as RecordModel]
  }
  return []
}

export function RelationInput({ field, value, onChange, collections, recordExpand, recordId }: FieldInputProps) {
  const opts = field.options ?? field
  const maxSelect = opts.maxSelect ?? 1
  const collectionId = opts.collectionId
  const isMulti = maxSelect > 1
  const isEditing = !!recordId

  const targetCollection = collections?.find((c) => c.id === collectionId)
  const targetName = targetCollection?.name ?? collectionId ?? 'unknown'

  const [pickerOpen, setPickerOpen] = useState(false)

  const ids = useMemo(() => parseIds(value, isMulti), [value, isMulti])

  const expandedRecords = useMemo(
    () => toExpandedRecords(recordExpand?.[field.name]),
    [recordExpand, field.name],
  )

  const displayItems = useMemo(() => {
    if (expandedRecords.length > 0) {
      const expandedById = new Map(expandedRecords.map((record) => [record.id, record]))
      const matched = ids
        .map((id) => expandedById.get(id))
        .filter((record): record is RecordModel => !!record)

      if (matched.length === ids.length && matched.length > 0) {
        return matched.map((record) => ({
          id: record.id,
          label: getRelationDisplayValue(record) ?? truncateId(record.id),
        }))
      }
    }

    return ids.map((id) => ({ id, label: truncateId(id) }))
  }, [expandedRecords, ids])

  const hasCollection = !!collectionId

  const handleOpenRelation = (recordId: string) => {
    if (!collectionId) return
    const targetUrl = `${window.location.origin}/_/collections/${encodeURIComponent(collectionId)}?record=${encodeURIComponent(recordId)}`
    window.open(targetUrl, '_blank', 'noopener,noreferrer')
  }

  const handlePickerConfirm = (selectedIds: string[]) => {
    if (isMulti) {
      onChange(selectedIds)
      return
    }
    onChange(selectedIds[0] ?? '')
  }

  if (isMulti) {
    return (
      <div className="space-y-2.5">
        <div className="flex items-center gap-2">
          <Label htmlFor={field.name}>{field.name}</Label>
          {field.required && <span className="text-destructive text-sm">*</span>}
          <Badge variant="secondary" className="text-xs">relation</Badge>
        </div>

        <div className="flex items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => setPickerOpen(true)}
            disabled={!hasCollection}
          >
            Select records
          </Button>
          <span className="text-xs text-muted-foreground">
            {ids.length} selected
          </span>
        </div>

        <Textarea
          id={field.name}
          rows={3}
          value={ids.join(', ')}
          onChange={(e) => {
            const parsed = e.target.value
              .split(',')
              .map((s) => s.trim())
              .filter(Boolean)
            onChange(parsed)
          }}
          placeholder="Enter record IDs separated by commas..."
          readOnly={isEditing}
          disabled={isEditing}
        />

        <p className="text-xs text-muted-foreground">
          Target collection: {targetName}
        </p>

        {displayItems.length > 0 && (
          <div className="space-y-1">
            <p className="text-xs font-medium text-slate-600">Selected relations</p>
            <div className="flex flex-wrap gap-1.5">
              {displayItems.map((item) => (
                <button
                  key={`${field.name}-${item.id}`}
                  type="button"
                  className="inline-flex max-w-full items-center gap-1 rounded-md border border-slate-200 px-2 py-0.5 text-xs text-slate-700 hover:border-slate-300 hover:bg-slate-50"
                  onClick={() => handleOpenRelation(item.id)}
                  disabled={!hasCollection}
                  title={`Open related record ${item.id}`}
                >
                  <span className="truncate">{item.label}</span>
                  <ExternalLink className="h-3 w-3 shrink-0" />
                </button>
              ))}
            </div>
          </div>
        )}

        <RelationPickerDialog
          open={pickerOpen}
          onOpenChange={setPickerOpen}
          targetCollectionId={collectionId}
          collections={collections}
          isMulti
          selectedIds={ids}
          onConfirm={handlePickerConfirm}
        />
      </div>
    )
  }

  const singleValue = ids[0] ?? ''

  return (
    <div className="space-y-2.5">
      <Label htmlFor={field.name}>
        {field.name}
        {field.required && <span className="text-destructive ml-1">*</span>}
      </Label>

      <div className="flex items-center gap-2">
        <Input
          id={field.name}
          type="text"
          value={singleValue}
          onChange={(e) => onChange(e.target.value.trim())}
          placeholder="Enter record ID..."
          readOnly={isEditing}
          disabled={isEditing}
        />
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => setPickerOpen(true)}
          disabled={!hasCollection}
        >
          Select
        </Button>
      </div>

      <p className="text-xs text-muted-foreground">
        Target collection: {targetName}
      </p>

      {displayItems[0] && (
        <div className="space-y-1">
          <p className="text-xs font-medium text-slate-600">Selected relation</p>
          <button
            type="button"
            className="inline-flex max-w-full items-center gap-1 rounded-md border border-slate-200 px-2 py-0.5 text-xs text-slate-700 hover:border-slate-300 hover:bg-slate-50"
            onClick={() => handleOpenRelation(displayItems[0]!.id)}
            disabled={!hasCollection}
            title={`Open related record ${displayItems[0]!.id}`}
          >
            <span className="truncate">{displayItems[0]!.label}</span>
            <ExternalLink className="h-3 w-3 shrink-0" />
          </button>
        </div>
      )}

      <RelationPickerDialog
        open={pickerOpen}
        onOpenChange={setPickerOpen}
        targetCollectionId={collectionId}
        collections={collections}
        isMulti={false}
        selectedIds={singleValue ? [singleValue] : []}
        onConfirm={handlePickerConfirm}
      />
    </div>
  )
}
