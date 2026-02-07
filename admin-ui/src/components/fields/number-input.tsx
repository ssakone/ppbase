import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import type { FieldInputProps } from '@/components/record-field-input'

export function NumberInput({ field, value, onChange }: FieldInputProps) {
  const opts = field.options ?? field
  const onlyInt = opts.onlyInt
  const min = opts.min
  const max = opts.max

  return (
    <div className="space-y-1.5">
      <Label htmlFor={field.name}>
        {field.name}
        {field.required && <span className="text-destructive ml-1">*</span>}
      </Label>
      <Input
        id={field.name}
        type="number"
        step={onlyInt ? 1 : 'any'}
        min={min}
        max={max}
        value={value === null || value === undefined ? '' : String(value)}
        onChange={(e) => {
          const v = e.target.value
          if (v === '') {
            onChange(null)
          } else {
            onChange(onlyInt ? parseInt(v, 10) : parseFloat(v))
          }
        }}
        placeholder="0"
      />
    </div>
  )
}
