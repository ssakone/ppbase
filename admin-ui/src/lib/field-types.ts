export interface FieldTypeConfig {
  label: string
  icon: string
  bg: string
  color: string
  badgeClass: string
}

export const FIELD_TYPES: string[] = [
  'text', 'editor', 'number', 'bool',
  'email', 'url', 'date', 'select',
  'json', 'file', 'relation',
]

export const FIELD_TYPE_CONFIG: Record<string, FieldTypeConfig> = {
  // User field types
  text:     { label: 'Plain text',  icon: 'T',  bg: 'var(--field-text-bg)', color: 'var(--field-text-fg)', badgeClass: 'bg-blue-100 text-blue-700' },
  editor:   { label: 'Rich editor', icon: 'R',  bg: 'var(--field-editor-bg)', color: 'var(--field-editor-fg)', badgeClass: 'bg-slate-100 text-slate-600' },
  number:   { label: 'Number',      icon: '#',  bg: 'var(--field-number-bg)', color: 'var(--field-number-fg)', badgeClass: 'bg-purple-100 text-purple-700' },
  bool:     { label: 'Bool',        icon: '◉',  bg: 'var(--field-bool-bg)', color: 'var(--field-bool-fg)', badgeClass: 'bg-green-100 text-green-700' },
  email:    { label: 'Email',       icon: '@',  bg: 'var(--field-email-bg)', color: 'var(--field-email-fg)', badgeClass: 'bg-indigo-100 text-indigo-700' },
  url:      { label: 'URL',         icon: 'U',  bg: 'var(--field-email-bg)', color: 'var(--field-email-fg)', badgeClass: 'bg-indigo-100 text-indigo-700' },
  date:     { label: 'Datetime',    icon: '⊞',  bg: 'var(--field-date-bg)', color: 'var(--field-date-fg)', badgeClass: 'bg-amber-100 text-amber-700' },
  select:   { label: 'Select',      icon: 'S',  bg: 'var(--field-select-bg)', color: 'var(--field-select-fg)', badgeClass: 'bg-purple-100 text-purple-700' },
  json:     { label: 'JSON',        icon: 'J',  bg: 'var(--field-json-bg)', color: 'var(--field-json-fg)', badgeClass: 'bg-green-100 text-green-700' },
  file:     { label: 'File',        icon: 'F',  bg: 'var(--field-file-bg)', color: 'var(--field-file-fg)', badgeClass: 'bg-orange-100 text-orange-700' },
  relation: { label: 'Relation',    icon: 'L',  bg: 'var(--field-relation-bg)', color: 'var(--field-relation-fg)', badgeClass: 'bg-red-100 text-red-700' },
  // System / special field types
  autoId:   { label: 'ID',          icon: '~>', bg: 'var(--field-id-bg)', color: 'var(--field-id-fg)', badgeClass: 'bg-slate-100 text-slate-600' },
  autodate: { label: 'Autodate',    icon: '⊞',  bg: 'var(--field-number-bg)', color: 'var(--field-number-fg)', badgeClass: 'bg-purple-100 text-purple-600' },
}

/** Return the field type config key for a system column. */
export function getSystemColumnType(name: string): string {
  if (name === 'id') return 'autoId'
  if (name === 'created' || name === 'updated') return 'autodate'
  return 'text'
}
