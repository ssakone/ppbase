import type { Collection, Field } from '../api/types'

const AUTH_SYSTEM_FIELDS: Field[] = [
  { name: 'email', type: 'email' },
  { name: 'emailVisibility', type: 'bool' },
  { name: 'verified', type: 'bool' },
]

const AUTH_INTERNAL_FIELD_NAMES = new Set(['password', 'tokenKey'])

export function isAuthCollection(collection: Collection): boolean {
  return collection.type === 'auth' || collection.name === '_superusers'
}

export function getRecordEditorFields(collection: Collection): Field[] {
  const fields = collection.fields ?? collection.schema ?? []
  if (!isAuthCollection(collection)) return fields

  const existing = new Set(fields.map((field) => field.name))
  const authSystemFields = collection.name === '_superusers'
    ? AUTH_SYSTEM_FIELDS.filter((field) => field.name === 'email')
    : AUTH_SYSTEM_FIELDS

  return [
    ...fields,
    ...authSystemFields.filter((field) => !existing.has(field.name)),
  ]
}

export function getEditableRecordFields(collection: Collection, fields: Field[]): Field[] {
  if (!isAuthCollection(collection)) return fields

  return fields.filter((field) => {
    if (AUTH_INTERNAL_FIELD_NAMES.has(field.name)) return false
    if (collection.name === '_superusers' && field.name === 'verified') return false
    return true
  })
}

export function buildRecordFormData(
  fields: Field[],
  source?: Record<string, unknown> | null,
  resetPrimaryKey = false,
): Record<string, unknown> {
  const data: Record<string, unknown> = {}

  for (const field of fields) {
    if (resetPrimaryKey && (field.primaryKey || field.name === 'id')) {
      data[field.name] = null
    } else if (source) {
      data[field.name] = source[field.name] ?? null
    } else {
      data[field.name] = field.type === 'bool' ? false : null
    }
  }

  return data
}

export function getPasswordMinimum(fields: Field[]): number {
  const password = fields.find((field) => field.name === 'password' && field.type === 'password')
  return Number(password?.min ?? password?.options?.min ?? 8) || 8
}
