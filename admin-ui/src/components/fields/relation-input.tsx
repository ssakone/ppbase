import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import type { FieldInputProps } from '@/components/record-field-input'

export function RelationInput({ field, value, onChange, collections }: FieldInputProps) {
  const opts = field.options ?? field
  const maxSelect = opts.maxSelect ?? 1
  const collectionId = opts.collectionId
  const isMulti = maxSelect > 1

  const targetCollection = collections?.find((c) => c.id === collectionId)
  const targetName = targetCollection?.name ?? collectionId ?? 'unknown'

  if (isMulti) {
    const ids: string[] = Array.isArray(value)
      ? value.map(String)
      : value
        ? String(value).split(',').map((s) => s.trim()).filter(Boolean)
        : []

    return (
      <div className="space-y-1.5">
        <div className="flex items-center gap-2">
          <Label htmlFor={field.name}>{field.name}</Label>
          {field.required && <span className="text-destructive text-sm">*</span>}
          <Badge variant="secondary" className="text-xs">relation</Badge>
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
        />
        <p className="text-xs text-muted-foreground">
          Target collection: {targetName}
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-1.5">
      <Label htmlFor={field.name}>
        {field.name}
        {field.required && <span className="text-destructive ml-1">*</span>}
      </Label>
      <Input
        id={field.name}
        type="text"
        value={(value as string) ?? ''}
        onChange={(e) => onChange(e.target.value)}
        placeholder="Enter record ID..."
      />
      <p className="text-xs text-muted-foreground">
        Target collection: {targetName}
      </p>
    </div>
  )
}
