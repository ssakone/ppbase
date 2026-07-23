export function isBackupTrusted(
  trustStatus: string | null | undefined,
  authenticated: boolean | undefined,
): boolean {
  if (trustStatus === 'trusted_local' || trustStatus === 'trusted_external') return true

  // Older PPBase responses may omit trustStatus and expose only the verified
  // signature bit. Any explicit status, including a future unknown value,
  // must fail closed until the UI understands it.
  return trustStatus == null && authenticated === true
}
