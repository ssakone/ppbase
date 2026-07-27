import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  approveBackupSigner,
  createBackup,
  deleteBackup,
  getBackupIdentity,
  getBackupReadiness,
  inspectBackup,
  listBackups,
  listBackupTrust,
  restoreBackupDestructive,
  revokeBackupSigner,
  uploadBackup,
} from '@/api/endpoints/backups'
import type { UploadProgress } from '@/api/client'

export function useBackups() {
  return useQuery({ queryKey: ['backups'], queryFn: listBackups, retry: false })
}

export function useBackup(id?: string, resourceOffset = 0, resourceLimit = 100) {
  return useQuery({
    queryKey: ['backups', id, 'resources', resourceOffset, resourceLimit],
    queryFn: () => inspectBackup(id!, resourceOffset, resourceLimit),
    enabled: !!id,
    retry: false,
  })
}

export function useBackupIdentity() {
  return useQuery({ queryKey: ['backup-identity'], queryFn: getBackupIdentity, retry: false })
}

export function useBackupReadiness() {
  return useQuery({ queryKey: ['backup-readiness'], queryFn: getBackupReadiness, retry: false })
}

export function useBackupTrust() {
  return useQuery({ queryKey: ['backup-trust'], queryFn: listBackupTrust, retry: false })
}

function useInvalidateBackups() {
  const queryClient = useQueryClient()
  return () => {
    void queryClient.invalidateQueries({ queryKey: ['backups'] })
    void queryClient.invalidateQueries({ queryKey: ['backup-trust'] })
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

export function useApproveBackupSigner() {
  const invalidate = useInvalidateBackups()
  return useMutation({
    mutationFn: ({ id, fingerprintSha256 }: {
      id: string
      fingerprintSha256: string
    }) => approveBackupSigner(id, fingerprintSha256),
    onSuccess: invalidate,
  })
}

export function useRevokeBackupSigner() {
  const invalidate = useInvalidateBackups()
  return useMutation({ mutationFn: revokeBackupSigner, onSuccess: invalidate })
}

export function useRestoreBackupDestructive() {
  return useMutation({ mutationFn: (id: string) => restoreBackupDestructive(id) })
}
