import { Textarea } from '@/components/ui/textarea'
import { Label } from '@/components/ui/label'
import type { FieldInputProps } from '@/components/record-field-input'

export function EditorInput({ field, value, onChange }: FieldInputProps) {
  return (
    <div className="space-y-1.5">
      <Label htmlFor={field.name}>
        {field.name}
        {field.required && <span className="text-destructive ml-1">*</span>}
      </Label>
      <Textarea
        id={field.name}
        rows={6}
        value={(value as string) ?? ''}
        onChange={(e) => onChange(e.target.value)}
        placeholder="Enter rich text content..."
      />
    </div>
  )
}
