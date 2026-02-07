import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'
import type { FieldInputProps } from '@/components/record-field-input'

export function BoolInput({ field, value, onChange }: FieldInputProps) {
  return (
    <div className="flex items-center space-x-2 py-2">
      <Checkbox
        id={field.name}
        checked={!!value}
        onCheckedChange={(checked) => onChange(!!checked)}
      />
      <Label htmlFor={field.name} className="cursor-pointer">
        {field.name}
      </Label>
    </div>
  )
}
