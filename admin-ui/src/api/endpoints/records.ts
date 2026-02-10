import { apiClient } from '../client'
import type { PaginatedResult, RecordModel } from '../types'

export async function listRecords(collection: string, params?: string): Promise<PaginatedResult<RecordModel>> {
  const query = params ? '?' + params : ''
  return apiClient.request<PaginatedResult<RecordModel>>(
    'GET',
    '/api/collections/' + encodeURIComponent(collection) + '/records' + query,
  )
}

export async function getRecord(collection: string, id: string): Promise<RecordModel> {
  return apiClient.request<RecordModel>(
    'GET',
    '/api/collections/' + encodeURIComponent(collection) + '/records/' + encodeURIComponent(id),
  )
}

function hasFiles(data: Record<string, unknown>): boolean {
  for (const value of Object.values(data)) {
    if (value instanceof File) return true
    if (Array.isArray(value) && value.some((v) => v instanceof File)) return true
  }
  return false
}

function buildFormData(data: Record<string, unknown>): FormData {
  const formData = new FormData()
  for (const [key, value] of Object.entries(data)) {
    if (value === null || value === undefined) continue
    if (value instanceof File) {
      formData.append(key, value)
    } else if (Array.isArray(value)) {
      for (const item of value) {
        if (item instanceof File) {
          formData.append(key, item)
        } else {
          formData.append(key, String(item))
        }
      }
    } else if (typeof value === 'object') {
      formData.append(key, JSON.stringify(value))
    } else {
      formData.append(key, String(value))
    }
  }
  return formData
}

export async function createRecord(collection: string, data: Record<string, unknown>): Promise<RecordModel> {
  const path = '/api/collections/' + encodeURIComponent(collection) + '/records'

  if (hasFiles(data)) {
    const formData = buildFormData(data)
    return apiClient.requestFormData<RecordModel>('POST', path, formData)
  }

  return apiClient.request<RecordModel>('POST', path, data)
}

export async function updateRecord(
  collection: string,
  id: string,
  data: Record<string, unknown>,
): Promise<RecordModel> {
  const path = '/api/collections/' + encodeURIComponent(collection) + '/records/' + encodeURIComponent(id)

  console.log('[records.ts] updateRecord:', { collection, id, hasFiles: hasFiles(data), data })

  if (hasFiles(data)) {
    const formData = buildFormData(data)
    console.log('[records.ts] Sending FormData, entries:', [...formData.entries()])
    return apiClient.requestFormData<RecordModel>('PATCH', path, formData)
  }

  console.log('[records.ts] Sending JSON:', data)
  return apiClient.request<RecordModel>('PATCH', path, data)
}

export async function deleteRecord(collection: string, id: string): Promise<void> {
  return apiClient.request<void>(
    'DELETE',
    '/api/collections/' + encodeURIComponent(collection) + '/records/' + encodeURIComponent(id),
  )
}

