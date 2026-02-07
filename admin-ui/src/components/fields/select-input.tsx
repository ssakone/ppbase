import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import type { FieldInputProps } from '@/components/record-field-input'

export function SelectInput({ field, value, onChange }: FieldInputProps) {
  const opts = field.options ?? field
  const values = opts.values ?? []
  const maxSelect = opts.maxSelect ?? 1
  const isMulti = maxSelect > 1

  if (values.length === 0) {
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
          placeholder="Enter value..."
        />
        <p className="text-xs text-muted-foreground">No select values configured</p>
      </div>
    )
  }

  if (isMulti) {
    const selected: string[] = Array.isArray(value) ? value : value ? [String(value)] : []

    const toggle = (v: string) => {
      const next = selected.includes(v)
        ? selected.filter((s) => s !== v)
        : [...selected, v]
      onChange(next)
    }

    return (
      <div className="space-y-1.5">
        <div className="flex items-center gap-2">
          <Label>{field.name}</Label>
          {field.required && <span className="text-destructive text-sm">*</span>}
          <Badge variant="secondary" className="text-xs">multi</Badge>
        </div>
        <div className="space-y-1.5 rounded-md border p-3">
          {values.map((v) => (
            <div key={v} className="flex items-center space-x-2">
              <Checkbox
                id={`${field.name}-${v}`}
                checked={selected.includes(v)}
                onCheckedChange={() => toggle(v)}
              />
              <Label htmlFor={`${field.name}-${v}`} className="cursor-pointer font-normal">
                {v}
              </Label>
            </div>
          ))}
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-1.5">
      <Label htmlFor={field.name}>
        {field.name}
        {field.required && <span className="text-destructive ml-1">*</span>}
      </Label>
      <Select
        value={(value as string) ?? ''}
        onValueChange={(v) => onChange(v)}
      >
        <SelectTrigger id={field.name}>
          <SelectValue placeholder="Select a value..." />
        </SelectTrigger>
        <SelectContent>
          {values.map((v) => (
            <SelectItem key={v} value={v}>
              {v}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  )
}
