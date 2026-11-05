import { apiClient } from '../client'
import type { SqlExecuteResult } from '../types'

export interface SqlExecuteParams {
  query: string
  confirm?: boolean
  maxRows?: number
}

export async function executeSql(params: SqlExecuteParams): Promise<SqlExecuteResult> {
  return apiClient.request<SqlExecuteResult>('POST', '/api/sql/execute', params)
}
