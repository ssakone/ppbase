import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  listRecords,
  getRecord,
  createRecord,
  updateRecord,
  deleteRecord,
} from '@/api/endpoints/records'

interface UseRecordsParams {
  page?: number
  perPage?: number
  filter?: string
  sort?: string
  expand?: string
}

function serializeParams(params?: UseRecordsParams): string | undefined {
  if (!params) return undefined
  const parts: string[] = []
  if (params.page) parts.push(`page=${params.page}`)
  if (params.perPage) parts.push(`perPage=${params.perPage}`)
  if (params.filter) parts.push(`filter=${encodeURIComponent(params.filter)}`)
  if (params.sort) parts.push(`sort=${encodeURIComponent(params.sort)}`)
  if (params.expand) parts.push(`expand=${encodeURIComponent(params.expand)}`)
  return parts.length > 0 ? parts.join('&') : undefined
}

export function useRecords(collectionIdOrName: string | undefined, params?: UseRecordsParams) {
  const query = serializeParams(params)
  return useQuery({
    queryKey: ['records', collectionIdOrName, query],
    queryFn: () => listRecords(collectionIdOrName!, query),
    enabled: !!collectionIdOrName,
  })
}

interface UseRecordParams {
  expand?: string
}

export function useRecord(
  collectionIdOrName: string | undefined,
  id: string | undefined,
  params?: UseRecordParams,
) {
  const query = serializeParams(params)
  return useQuery({
    queryKey: ['records', collectionIdOrName, id, query],
    queryFn: () => getRecord(collectionIdOrName!, id!, query),
    enabled: !!collectionIdOrName && !!id,
  })
}

export function useCreateRecord(collectionIdOrName: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (data: Record<string, unknown>) => createRecord(collectionIdOrName, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['records', collectionIdOrName] })
    },
  })
}

export function useUpdateRecord(collectionIdOrName: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, data }: { id: string; data: Record<string, unknown> }) =>
      updateRecord(collectionIdOrName, id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['records', collectionIdOrName] })
    },
  })
}

export function useDeleteRecord(collectionIdOrName: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => deleteRecord(collectionIdOrName, id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['records', collectionIdOrName] })
    },
  })
}
