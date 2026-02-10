import type { Field, Collection } from '@/api/types'
import { TextInput } from './fields/text-input'
import { NumberInput } from './fields/number-input'
import { EmailInput } from './fields/email-input'
import { UrlInput } from './fields/url-input'
import { DateInput } from './fields/date-input'
import { BoolInput } from './fields/bool-input'
import { JsonInput } from './fields/json-input'
import { EditorInput } from './fields/editor-input'
import { SelectInput } from './fields/select-input'
import { RelationInput } from './fields/relation-input'
import { FileInput } from './fields/file-input'

export interface FieldInputProps {
  field: Field
  value: unknown
  onChange: (value: unknown) => void
  collections?: Collection[]
  recordId?: string
  collectionId?: string
}

const FIELD_COMPONENTS: Record<string, React.FC<FieldInputProps>> = {
  text: TextInput,
  editor: EditorInput,
  number: NumberInput,
  bool: BoolInput,
  email: EmailInput,
  url: UrlInput,
  date: DateInput,
  select: SelectInput,
  json: JsonInput,
  file: FileInput,
  relation: RelationInput,
}

interface RecordFieldInputProps extends FieldInputProps { }

export function RecordFieldInput(props: RecordFieldInputProps) {
  const Component = FIELD_COMPONENTS[props.field.type]

  if (!Component) {
    return (
      <div className="space-y-1.5">
        <label className="text-sm font-medium">{props.field.name}</label>
        <p className="text-xs text-muted-foreground">
          Unsupported field type: {props.field.type}
        </p>
      </div>
    )
  }

  return <Component {...props} />
}
