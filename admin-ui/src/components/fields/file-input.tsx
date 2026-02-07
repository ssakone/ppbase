import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import type { FieldInputProps } from '@/components/record-field-input'

export function FileInput({ field }: FieldInputProps) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-2">
        <Label htmlFor={field.name}>{field.name}</Label>
        {field.required && <span className="text-destructive text-sm">*</span>}
        <Badge variant="secondary" className="text-xs">file</Badge>
      </div>
      <Input
        id={field.name}
        type="text"
        disabled
        placeholder="File uploads not yet supported"
      />
      <p className="text-xs text-muted-foreground">
        File upload support is not yet implemented.
      </p>
    </div>
  )
}
