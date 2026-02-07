import type { Field } from '@/api/types'
import { truncateId } from './utils'

export function formatCellValue(value: unknown, field: Field | string): string {
  if (value === null || value === undefined) return '-'
  const type = typeof field === 'string' ? field : field.type

  switch (type) {
    case 'bool':
      return value ? 'Yes' : 'No'

    case 'json':
      if (typeof value === 'object') {
        const str = JSON.stringify(value)
        return str.length > 50 ? str.substring(0, 50) + '...' : str
      }
      return String(value)

    case 'select': {
      if (Array.isArray(value)) {
        return value.join(', ')
      }
      return String(value)
    }

    case 'relation':
      if (Array.isArray(value)) {
        return value.length + ' relation' + (value.length !== 1 ? 's' : '')
      }
      return truncateId(String(value))

    case 'file':
      if (Array.isArray(value)) {
        return value.length + ' file' + (value.length !== 1 ? 's' : '')
      }
      return String(value)

    default: {
      const str = String(value)
      return str.length > 60 ? str.substring(0, 60) + '...' : str
    }
  }
}
