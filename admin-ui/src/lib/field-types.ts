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
  text:     { label: 'Plain text',  icon: 'T', bg: '#eff6ff', color: '#1d4ed8', badgeClass: 'bg-blue-100 text-blue-700' },
  editor:   { label: 'Rich editor', icon: 'R', bg: '#f8fafc', color: '#475569', badgeClass: 'bg-slate-100 text-slate-600' },
  number:   { label: 'Number',      icon: '#', bg: '#f5f3ff', color: '#7c3aed', badgeClass: 'bg-purple-100 text-purple-700' },
  bool:     { label: 'Bool',        icon: 'B', bg: '#ecfdf5', color: '#059669', badgeClass: 'bg-green-100 text-green-700' },
  email:    { label: 'Email',       icon: '@', bg: '#eef2ff', color: '#4f46e5', badgeClass: 'bg-indigo-100 text-indigo-700' },
  url:      { label: 'URL',         icon: 'U', bg: '#eef2ff', color: '#4f46e5', badgeClass: 'bg-indigo-100 text-indigo-700' },
  date:     { label: 'Datetime',    icon: 'D', bg: '#fffbeb', color: '#d97706', badgeClass: 'bg-amber-100 text-amber-700' },
  select:   { label: 'Select',      icon: 'S', bg: '#fdf4ff', color: '#a855f7', badgeClass: 'bg-purple-100 text-purple-700' },
  json:     { label: 'JSON',        icon: 'J', bg: '#f0fdf4', color: '#16a34a', badgeClass: 'bg-green-100 text-green-700' },
  file:     { label: 'File',        icon: 'F', bg: '#fff7ed', color: '#ea580c', badgeClass: 'bg-orange-100 text-orange-700' },
  relation: { label: 'Relation',    icon: 'L', bg: '#fef2f2', color: '#dc2626', badgeClass: 'bg-red-100 text-red-700' },
}
