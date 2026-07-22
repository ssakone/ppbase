import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  abandonBackupStagingPlan,
  activateBackupStagingPlan,
  approveBackupSigner,
  createBackup,
  createBackupStagingPlan,
  deleteBackup,
  executeBackupStagingPlan,
  getBackupActivation,
  getBackupIdentity,
  getBackupReadiness,
  inspectBackupStagingPlan,
  inspectBackup,
  listBackups,
  listBackupTrust,
  revokeBackupSigner,
  uploadBackup,
} from '@/api/endpoints/backups'
import type { JwtSecretMode } from '@/api/types'
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

export function useCreateBackupStagingPlan() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, jwtSecretMode }: { id: string; jwtSecretMode: JwtSecretMode }) => (
      createBackupStagingPlan(id, jwtSecretMode)
    ),
    onSuccess: (plan) => {
      queryClient.setQueryData(['backup-staging', plan.id], plan)
    },
  })
}

export function useExecuteBackupStagingPlan() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ planId, planHash }: { planId: string; planHash: string }) => (
      executeBackupStagingPlan(planId, planHash)
    ),
    onSuccess: (plan) => {
      queryClient.setQueryData(['backup-staging', plan.id], plan)
    },
    onSettled: (_data, _error, variables) => {
      void queryClient.invalidateQueries({ queryKey: ['backup-staging', variables.planId] })
    },
  })
}

export function useBackupStagingPlan(planId?: string) {
  return useQuery({
    queryKey: ['backup-staging', planId],
    queryFn: () => inspectBackupStagingPlan(planId!),
    enabled: !!planId,
    retry: false,
    refetchInterval: (query) => query.state.data?.status === 'running' ? 1500 : false,
    refetchIntervalInBackground: true,
  })
}

export function useAbandonBackupStagingPlan() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ planId, planHash }: { planId: string; planHash: string }) => (
      abandonBackupStagingPlan(planId, planHash)
    ),
    onSuccess: (_data, variables) => {
      queryClient.removeQueries({ queryKey: ['backup-staging', variables.planId] })
    },
  })
}

export function useActivateBackupStagingPlan() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ planId, planHash, activationId, resumeToken }: {
      planId: string
      planHash: string
      activationId: string
      resumeToken: string
    }) => (
      activateBackupStagingPlan(planId, planHash, activationId, resumeToken)
    ),
    onSettled: (_data, _error, variables) => {
      void queryClient.invalidateQueries({
        queryKey: ['backup-activation', variables.activationId],
      })
    },
  })
}

const TERMINAL_ACTIVATION_STATUSES = new Set([
  'succeeded',
  'rolled_back',
  'failed',
  'action_required',
])

export function useBackupActivation(activationId?: string, resumeToken?: string) {
  return useQuery({
    queryKey: ['backup-activation', activationId],
    queryFn: () => getBackupActivation(activationId!, resumeToken!),
    enabled: !!activationId && !!resumeToken,
    retry: false,
    refetchInterval: (query) => {
      const status = query.state.data?.status
      return status && TERMINAL_ACTIVATION_STATUSES.has(status) ? false : 1500
    },
    refetchIntervalInBackground: true,
  })
}
