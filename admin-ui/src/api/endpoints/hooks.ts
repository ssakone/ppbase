import { apiClient } from '../client'
import type { HookState, HooksListResult, HooksRescanResult, HooksRuntime } from '../types'

export async function getHooks(): Promise<HooksListResult> {
  return apiClient.request<HooksListResult>('GET', '/api/hooks')
}

export async function getHook(hookId: string): Promise<HookState> {
  return apiClient.request<HookState>('GET', `/api/hooks/${hookId}`)
}

export async function updateHook(
  hookId: string,
  data: { enabled: boolean },
): Promise<HookState> {
  return apiClient.request<HookState>('PATCH', `/api/hooks/${hookId}`, data)
}

export async function rescanHooks(): Promise<HooksRescanResult> {
  return apiClient.request<HooksRescanResult>('POST', '/api/hooks/rescan')
}

export async function reloadHook(hookId: string): Promise<HookState> {
  return apiClient.request<HookState>('POST', `/api/hooks/${hookId}/reload`)
}

export async function updateHooksRuntime(
  data: { autoRestartOnChange: boolean },
): Promise<HooksRuntime> {
  return apiClient.request<HooksRuntime>('PATCH', '/api/hooks/runtime', data)
}

export async function restartPPBase(): Promise<{ scheduled: boolean; canRestart: boolean; restartPending: boolean }> {
  return apiClient.request<{ scheduled: boolean; canRestart: boolean; restartPending: boolean }>(
    'POST',
    '/api/hooks/restart',
  )
}
