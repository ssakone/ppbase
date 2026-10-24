import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import type { FieldInputProps } from '@/components/record-field-input'

export function TextInput({ field, value, onChange, recordId }: FieldInputProps) {
  const isPrimaryKeyLocked = !!field.primaryKey && !!recordId
  const hasAutogenerate = !!field.autogeneratePattern && !recordId
  const isRequired = !!field.required && !hasAutogenerate && !isPrimaryKeyLocked

  return (
    <div className="space-y-1.5">
      <Label htmlFor={field.name}>
        {field.name}
        {isRequired && <span className="text-destructive ml-1">*</span>}
      </Label>
      <Input
        id={field.name}
        type="text"
        required={isRequired}
        disabled={isPrimaryKeyLocked}
        value={(value as string) ?? ''}
        onChange={(e) => onChange(e.target.value)}
        placeholder={hasAutogenerate ? 'Leave empty to autogenerate...' : `Enter ${field.name}...`}
      />
    </div>
  )
}
