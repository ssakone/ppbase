import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  listMigrations,
  getMigrationStatus,
  applyMigrations,
  revertMigration,
  generateSnapshot,
} from '@/api/endpoints/migrations'

export function useMigrations() {
  return useQuery({
    queryKey: ['migrations'],
    queryFn: listMigrations,
  })
}

export function useMigrationStatus() {
  return useQuery({
    queryKey: ['migrations', 'status'],
    queryFn: getMigrationStatus,
  })
}

export function useApplyMigrations() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: applyMigrations,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['migrations'] })
    },
  })
}

export function useRevertMigration() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (count?: number) => revertMigration(count),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['migrations'] })
    },
  })
}

export function useGenerateSnapshot() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: generateSnapshot,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['migrations'] })
    },
  })
}
