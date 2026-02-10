export interface Collection {
  id: string
  name: string
  type: 'base' | 'auth' | 'view'
  system?: boolean
  schema?: Field[]
  fields?: Field[]
  listRule: string | null
  viewRule: string | null
  createRule: string | null
  updateRule: string | null
  deleteRule: string | null
  options?: { [key: string]: unknown }
  created: string
  updated: string
}

export interface Field {
  name: string
  type: string
  required?: boolean
  options?: FieldOptions
  // Flat format fields (PocketBase v0.23+)
  min?: number
  max?: number
  pattern?: string
  onlyInt?: boolean
  values?: string[]
  maxSelect?: number
  collectionId?: string
  cascadeDelete?: boolean
  maxSize?: number
  mimeTypes?: string[]
  onlyDomains?: string[]
  exceptDomains?: string[]
  convertURLs?: boolean
}

export interface FieldOptions {
  min?: number
  max?: number
  pattern?: string
  onlyInt?: boolean
  values?: string[]
  maxSelect?: number
  collectionId?: string
  cascadeDelete?: boolean
  maxSize?: number
  mimeTypes?: string[]
  onlyDomains?: string[]
  exceptDomains?: string[]
  convertURLs?: boolean
  query?: string
}

export interface RecordModel {
  id: string
  collectionId?: string
  collectionName?: string
  created: string
  updated: string
  [key: string]: unknown
}

export interface PaginatedResult<T> {
  page: number
  perPage: number
  totalItems: number
  totalPages: number
  items: T[]
}

export interface AdminAuth {
  token: string
  admin: { id: string; email: string }
}

export interface Settings {
  meta?: { appName?: string; appUrl?: string }
  [key: string]: unknown
}

export interface Migration {
  file: string
  status: 'applied' | 'pending'
  applied: string | null
}

export interface MigrationStatus {
  total: number
  applied: number
  pending: number
  lastApplied: string | null
}

export interface DatabaseTable {
  name: string
  columns: { name: string; type: string }[]
}

export interface ApiError {
  status: number
  message: string
  data: { [key: string]: unknown }
}
