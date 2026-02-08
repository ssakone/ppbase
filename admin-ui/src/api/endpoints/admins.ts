import { apiClient } from '../client'

export interface ChangePasswordRequest {
    password: string
    passwordConfirm: string
}

export async function changeAdminPassword(
    adminId: string,
    data: ChangePasswordRequest
): Promise<void> {
    await apiClient.request(
        'POST',
        `/api/admins/${encodeURIComponent(adminId)}/change-password`,
        data
    )
}
