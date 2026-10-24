import type { Field } from '../api/types'

const REQUIRED_LOCKED_AUTH_FIELDS = new Set(['password', 'tokenKey'])
const HIDDEN_LOCKED_AUTH_FIELDS = new Set(['password', 'tokenKey', 'email'])

export function canDuplicateCollectionField(field: Field): boolean {
  return field.type !== 'password'
}

export function canRemoveCollectionField(field: Field): boolean {
  return field.system !== true
}

export function getLockedFieldOption(
  field: Field,
  collectionType: string,
  option: 'required' | 'hidden',
): boolean | undefined {
  if (!field.system) return undefined

  if (option === 'required') {
    if (field.name === 'id' || (
      collectionType === 'auth' && REQUIRED_LOCKED_AUTH_FIELDS.has(field.name)
    )) {
      return true
    }
    return undefined
  }

  if (field.name === 'id') return false
  if (collectionType === 'auth' && HIDDEN_LOCKED_AUTH_FIELDS.has(field.name)) {
    return field.name !== 'email'
  }
  return undefined
}

export function duplicateCollectionField(fields: Field[], index: number): Field[] {
  const source = fields[index]
  if (!source || !canDuplicateCollectionField(source)) return fields

  const names = new Set(fields.map((field) => field.name.toLowerCase()))
  const baseName = `${source.name}_copy`
  let name = baseName
  let suffix = 2
  while (names.has(name.toLowerCase())) {
    name = `${baseName}${suffix}`
    suffix += 1
  }

  const duplicate: Field = {
    ...structuredClone(source),
    id: '',
    name,
    system: false,
    primaryKey: false,
  }

  return [...fields.slice(0, index + 1), duplicate, ...fields.slice(index + 1)]
}
