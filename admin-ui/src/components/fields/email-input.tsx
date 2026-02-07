import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import type { FieldInputProps } from '@/components/record-field-input'

export function EmailInput({ field, value, onChange }: FieldInputProps) {
  return (
    <div className="space-y-1.5">
      <Label htmlFor={field.name}>
        {field.name}
        {field.required && <span className="text-destructive ml-1">*</span>}
      </Label>
      <Input
        id={field.name}
        type="email"
        value={(value as string) ?? ''}
        onChange={(e) => onChange(e.target.value)}
        placeholder="user@example.com"
      />
    </div>
  )
}
