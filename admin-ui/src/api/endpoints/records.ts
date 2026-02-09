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

export async function createRecord(collection: string, data: Record<string, unknown>): Promise<RecordModel> {
  return apiClient.request<RecordModel>(
    'POST',
    '/api/collections/' + encodeURIComponent(collection) + '/records',
    data,
  )
}

export async function updateRecord(
  collection: string,
  id: string,
  data: Record<string, unknown>,
): Promise<RecordModel> {
  return apiClient.request<RecordModel>(
    'PATCH',
    '/api/collections/' + encodeURIComponent(collection) + '/records/' + encodeURIComponent(id),
    data,
  )
}

export async function deleteRecord(collection: string, id: string): Promise<void> {
  return apiClient.request<void>(
    'DELETE',
    '/api/collections/' + encodeURIComponent(collection) + '/records/' + encodeURIComponent(id),
  )
}
