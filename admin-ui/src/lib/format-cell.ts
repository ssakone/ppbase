import type { Field, RecordModel } from '@/api/types'
import { truncateId } from './utils'

const RELATION_DISPLAY_KEYS = ['name', 'title', 'username', 'label', 'email', 'slug'] as const

type ExpandedRecord = Record<string, unknown> & { id?: string }

function toExpandedArray(expandValue: unknown): ExpandedRecord[] {
  if (Array.isArray(expandValue)) {
    return expandValue.filter((item): item is ExpandedRecord => !!item && typeof item === 'object')
  }
  if (expandValue && typeof expandValue === 'object') {
    return [expandValue as ExpandedRecord]
  }
  return []
}

export function getRelationDisplayValue(expanded: ExpandedRecord | undefined): string | null {
  if (!expanded) return null
  for (const key of RELATION_DISPLAY_KEYS) {
    const value = expanded[key]
    if (typeof value === 'string' && value.trim()) return value
  }
  if (typeof expanded.id === 'string' && expanded.id) {
    return truncateId(expanded.id)
  }
  return null
}

export function getRelationDisplayValues(record: RecordModel, fieldName: string): string[] {
  const expanded = toExpandedArray(record.expand?.[fieldName])
  const labels = expanded
    .map((item) => getRelationDisplayValue(item))
    .filter((value): value is string => !!value)

  if (labels.length > 0) return labels

  const rawValue = record[fieldName]
  if (Array.isArray(rawValue)) {
    return rawValue.map((id) => truncateId(String(id))).filter(Boolean)
  }
  if (rawValue) {
    return [truncateId(String(rawValue))]
  }

  return []
}

export function getRelationRecordIds(record: RecordModel, fieldName: string): string[] {
  const expanded = toExpandedArray(record.expand?.[fieldName])
    .map((item) => item.id)
    .filter((id): id is string => typeof id === 'string' && !!id)

  if (expanded.length > 0) return expanded

  const rawValue = record[fieldName]
  if (Array.isArray(rawValue)) {
    return rawValue.map((id) => String(id)).filter(Boolean)
  }
  if (rawValue) {
    return [String(rawValue)]
  }

  return []
}

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
