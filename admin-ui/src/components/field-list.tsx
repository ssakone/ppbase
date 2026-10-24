import { Field, Collection } from '@/api/types'
import { FieldEditorRow } from '@/components/field-editor-row'
import { duplicateCollectionField } from '@/lib/collection-field-behavior'

interface FieldListProps {
  fields: Field[]
  onChange: (fields: Field[]) => void
  collections: Collection[]
  collectionType: 'base' | 'auth' | 'view'
}

export function FieldList({ fields, onChange, collections, collectionType }: FieldListProps) {
  const handleFieldChange = (index: number, updated: Field) => {
    const next = [...fields]
    next[index] = updated
    onChange(next)
  }

  const handleFieldRemove = (index: number) => {
    onChange(fields.filter((_, i) => i !== index))
  }

  const handleFieldDuplicate = (index: number) => {
    onChange(duplicateCollectionField(fields, index))
  }

  return (
    <div>
      {fields.map((field, i) => (
        <FieldEditorRow
          key={`${field.type}-${i}`}
          field={field}
          onChange={(f) => handleFieldChange(i, f)}
          onDuplicate={() => handleFieldDuplicate(i)}
          onRemove={() => handleFieldRemove(i)}
          collections={collections}
          collectionType={collectionType}
        />
      ))}
    </div>
  )
}
