import { useState } from 'react'
import { Textarea } from '@/components/ui/textarea'
import { Label } from '@/components/ui/label'
import type { FieldInputProps } from '@/components/record-field-input'

export function JsonInput({ field, value, onChange }: FieldInputProps) {
  const stringified =
    typeof value === 'string'
      ? value
      : value !== null && value !== undefined
        ? JSON.stringify(value, null, 2)
        : ''

  const [text, setText] = useState(stringified)
  const [error, setError] = useState<string | null>(null)

  const handleChange = (raw: string) => {
    setText(raw)
    if (raw.trim() === '') {
      setError(null)
      onChange(null)
      return
    }
    try {
      const parsed = JSON.parse(raw)
      setError(null)
      onChange(parsed)
    } catch {
      setError('Invalid JSON')
    }
  }

  return (
    <div className="space-y-1.5">
      <Label htmlFor={field.name}>
        {field.name}
        {field.required && <span className="text-destructive ml-1">*</span>}
      </Label>
      <Textarea
        id={field.name}
        rows={4}
        value={text}
        onChange={(e) => handleChange(e.target.value)}
        placeholder="{}"
        className={error ? 'border-destructive' : ''}
      />
      {error && (
        <p className="text-xs text-destructive">{error}</p>
      )}
    </div>
  )
}
