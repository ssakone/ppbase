import { Field, Collection } from '@/api/types'
import { FieldEditorRow } from '@/components/field-editor-row'

interface FieldListProps {
  fields: Field[]
  onChange: (fields: Field[]) => void
  collections: Collection[]
}

export function FieldList({ fields, onChange, collections }: FieldListProps) {
  const handleFieldChange = (index: number, updated: Field) => {
    const next = [...fields]
    next[index] = updated
    onChange(next)
  }

  const handleFieldRemove = (index: number) => {
    onChange(fields.filter((_, i) => i !== index))
  }

  return (
    <div>
      {fields.map((field, i) => (
        <FieldEditorRow
          key={`${field.type}-${i}`}
          field={field}
          onChange={(f) => handleFieldChange(i, f)}
          onRemove={() => handleFieldRemove(i)}
          collections={collections}
        />
      ))}
    </div>
  )
}
