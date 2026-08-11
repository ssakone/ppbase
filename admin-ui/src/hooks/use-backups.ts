import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  createBackup,
  deleteBackup,
  getBackupReadiness,
  listBackups,
  restoreBackupDestructive,
  uploadBackup,
} from '@/api/endpoints/backups'
import { getHealth } from '@/api/endpoints/health'
import type { UploadProgress } from '@/api/client'
import { BACKUP_STATUS_POLL_MS } from '@/lib/backup-operation-view'

export function useBackups() {
  return useQuery({ queryKey: ['backups'], queryFn: listBackups, retry: false })
}

export function useBackupReadiness() {
  return useQuery({ queryKey: ['backup-readiness'], queryFn: getBackupReadiness, retry: false })
}

export function useBackupOperationAvailability() {
  return useQuery({
    queryKey: ['health'],
    queryFn: getHealth,
    retry: false,
    refetchInterval: BACKUP_STATUS_POLL_MS,
    refetchOnWindowFocus: true,
  })
}

function useInvalidateBackups() {
  const queryClient = useQueryClient()
  return () => {
    void queryClient.invalidateQueries({ queryKey: ['backups'] })
  }
}

export function useCreateBackup() {
  const invalidate = useInvalidateBackups()
  return useMutation({ mutationFn: (name?: string) => createBackup(name), onSuccess: invalidate })
}

export function useUploadBackup() {
  const invalidate = useInvalidateBackups()
  return useMutation({
    mutationFn: ({ file, onProgress, signal }: {
      file: File
      onProgress?: (progress: UploadProgress) => void
      signal?: AbortSignal
    }) => uploadBackup(file, onProgress, signal),
    onSuccess: invalidate,
  })
}

export function useDeleteBackup() {
  const invalidate = useInvalidateBackups()
  return useMutation({ mutationFn: (id: string) => deleteBackup(id), onSuccess: invalidate })
}

export function useRestoreBackupDestructive() {
  return useMutation({ mutationFn: (id: string) => restoreBackupDestructive(id) })
}
