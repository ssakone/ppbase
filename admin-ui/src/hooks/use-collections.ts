import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  listCollections,
  getCollection,
  createCollection,
  updateCollection,
  deleteCollection,
} from '@/api/endpoints/collections'

export function useCollections() {
  return useQuery({
    queryKey: ['collections'],
    queryFn: () => listCollections('perPage=200'),
    select: (data) => Array.isArray(data) ? data : data.items,
  })
}

export function useCollection(idOrName: string | undefined) {
  return useQuery({
    queryKey: ['collections', idOrName],
    queryFn: () => getCollection(idOrName!),
    enabled: !!idOrName,
  })
}

export function useCreateCollection() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: createCollection,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['collections'] })
    },
  })
}

export function useUpdateCollection() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ idOrName, data }: { idOrName: string; data: Record<string, unknown> }) =>
      updateCollection(idOrName, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['collections'] })
    },
  })
}

export function useDeleteCollection() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (idOrName: string) => deleteCollection(idOrName),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['collections'] })
    },
  })
}
