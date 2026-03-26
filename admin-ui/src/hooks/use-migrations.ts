import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  listMigrations,
  getMigrationStatus,
  applyMigrations,
  revertMigration,
  generateSnapshot,
  type MigrationsListParams,
} from '@/api/endpoints/migrations'

export function useMigrations(params: MigrationsListParams = {}) {
  const page = params.page ?? 1
  const perPage = params.perPage ?? 30

  return useQuery({
    queryKey: ['migrations', page, perPage],
    queryFn: () => listMigrations({ page, perPage }),
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
