import { apiClient } from '../client'
import type { Collection, DatabaseTable, PaginatedResult } from '../types'

export async function listCollections(params?: string): Promise<PaginatedResult<Collection> | Collection[]> {
  const query = params ? '?' + params : ''
  return apiClient.request<PaginatedResult<Collection> | Collection[]>('GET', '/api/collections' + query)
}

export async function getCollection(idOrName: string): Promise<Collection> {
  return apiClient.request<Collection>('GET', '/api/collections/' + encodeURIComponent(idOrName))
}

export async function createCollection(data: Partial<Collection>): Promise<Collection> {
  return apiClient.request<Collection>('POST', '/api/collections', data)
}

export async function updateCollection(idOrName: string, data: Partial<Collection>): Promise<Collection> {
  return apiClient.request<Collection>('PATCH', '/api/collections/' + encodeURIComponent(idOrName), data)
}

export async function deleteCollection(idOrName: string): Promise<void> {
  return apiClient.request<void>('DELETE', '/api/collections/' + encodeURIComponent(idOrName))
}

export async function getDatabaseTables(): Promise<DatabaseTable[]> {
  return apiClient.request<DatabaseTable[]>('GET', '/api/collections/meta/tables')
}
