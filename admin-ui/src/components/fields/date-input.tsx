import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import type { FieldInputProps } from '@/components/record-field-input'

export function DateInput({ field, value, onChange }: FieldInputProps) {
  const formatted = value
    ? String(value).replace(' ', 'T').substring(0, 16)
    : ''

  return (
    <div className="space-y-1.5">
      <Label htmlFor={field.name}>
        {field.name}
        {field.required && <span className="text-destructive ml-1">*</span>}
      </Label>
      <Input
        id={field.name}
        type="datetime-local"
        value={formatted}
        onChange={(e) => {
          const v = e.target.value
          onChange(v ? v.replace('T', ' ') + ':00.000Z' : '')
        }}
      />
    </div>
  )
}
