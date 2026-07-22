import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  getHooks,
  updateHook,
  rescanHooks,
  reloadHook,
  updateHooksRuntime,
  restartPPBase,
} from '@/api/endpoints/hooks'

export function useHooks() {
  return useQuery({
    queryKey: ['hooks'],
    queryFn: getHooks,
    retry: false,
  })
}

export function useUpdateHook() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ hookId, enabled }: { hookId: string; enabled: boolean }) =>
      updateHook(hookId, { enabled }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['hooks'] })
    },
  })
}

export function useRescanHooks() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: rescanHooks,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['hooks'] })
    },
  })
}

export function useReloadHook() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (hookId: string) => reloadHook(hookId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['hooks'] })
    },
  })
}

export function useUpdateHooksRuntime() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (data: { autoRestartOnChange: boolean }) => updateHooksRuntime(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['hooks'] })
    },
  })
}

export function useRestartPPBase() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: restartPPBase,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['hooks'] })
    },
  })
}
