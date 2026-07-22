import { useEffect, useMemo, useState } from 'react'
import { AlertTriangle, CheckCircle2, Copy, Database, RefreshCw, ShieldCheck } from 'lucide-react'
import { toast } from 'sonner'
import type { BackupListItem, BackupStagingPlan, JwtSecretMode } from '@/api/types'
import { backupKey, getBackupErrorMessage } from '@/api/endpoints/backups'
import {
  activationStartMatchesResume,
  createActivationResume,
  isCanonicalStagingStatus,
  isExplicitHttpError,
  type ActivationResumeState,
  type StagingResumeState,
} from '@/lib/backup-resume-state'
import {
  useAbandonBackupStagingPlan,
  useActivateBackupStagingPlan,
  useApproveBackupSigner,
  useBackup,
  useBackupStagingPlan,
  useCreateBackupStagingPlan,
  useExecuteBackupStagingPlan,
} from '@/hooks/use-backups'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { LoadingSpinner } from '@/components/loading-spinner'

interface BackupRestoreWizardProps {
  backup: BackupListItem | null
  open: boolean
  onOpenChange: (open: boolean) => void
  stagingResume: StagingResumeState | null
  onStagingResumeChange: (state: StagingResumeState | null) => boolean
  onActivationResumeChange: (state: ActivationResumeState | null) => boolean
  onActivationAccepted: () => void
}

function isTrusted(trustStatus: string | undefined, authenticated: boolean | undefined): boolean {
  if (trustStatus === 'trusted_local' || trustStatus === 'trusted_external') return true
  return authenticated === true && !['authenticated_untrusted', 'revoked', 'unknown'].includes(
    trustStatus || '',
  )
}

function modeLabel(mode: JwtSecretMode) {
  return mode === 'clone' ? 'Clone' : 'Disaster recovery'
}

function errorStatus(error: unknown): number | null {
  if (!error || typeof error !== 'object') return null
  const status = (error as { status?: unknown }).status
  return typeof status === 'number' ? status : null
}

function stagingPlanMatchesResume(
  plan: BackupStagingPlan,
  resume: StagingResumeState,
): boolean {
  return plan.id === resume.planId
    && plan.planHash === resume.planHash
    && plan.backupId === resume.backupId
    && plan.jwtSecretMode === resume.mode
}

function stagingFailureMessage(plan: BackupStagingPlan): string {
  const status = plan.status === 'quarantined' ? 'quarantined' : 'failed'
  const code = typeof plan.failureCode === 'string' && plan.failureCode
    ? ` (${plan.failureCode})`
    : ''
  return `Restore staging ${status}${code}. The isolated targets were not activated.`
}

export function BackupRestoreWizard({
  backup,
  open,
  onOpenChange,
  stagingResume,
  onStagingResumeChange,
  onActivationResumeChange,
  onActivationAccepted,
}: BackupRestoreWizardProps) {
  const id = backup ? backupKey(backup) : undefined
  const { data: inspection, isLoading, isError, refetch } = useBackup(open ? id : undefined)
  const approveSigner = useApproveBackupSigner()
  const createPlan = useCreateBackupStagingPlan()
  const executePlan = useExecuteBackupStagingPlan()
  const abandonPlan = useAbandonBackupStagingPlan()
  const activatePlan = useActivateBackupStagingPlan()
  const resumedPlan = useBackupStagingPlan(stagingResume?.planId)

  const [mode, setMode] = useState<JwtSecretMode>('disaster_recovery')
  const [plan, setPlan] = useState<BackupStagingPlan | null>(null)
  const [validated, setValidated] = useState<BackupStagingPlan | null>(null)
  const [confirmation, setConfirmation] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const resumeMatchesBackup = !!id && stagingResume?.backupId === id

  useEffect(() => {
    setMode('disaster_recovery')
    setPlan(null)
    setValidated(null)
    setConfirmation('')
    setError('')
    setNotice('')
  }, [id, open])

  useEffect(() => {
    if (!stagingResume) return

    const restored = resumedPlan.data
    if (!restored && resumedPlan.isError) {
      if (resumeMatchesBackup) {
        setPlan(null)
        setValidated(null)
        setError(
          errorStatus(resumedPlan.error) === 404
            ? 'The saved staging plan no longer exists. Discard it before creating another plan.'
            : 'The saved staging plan could not be inspected. Retry before continuing.',
        )
      }
      return
    }

    if (!restored) return
    if (
      !stagingPlanMatchesResume(restored, stagingResume)
    ) {
      if (resumeMatchesBackup) {
        setPlan(null)
        setValidated(null)
        setError('The canonical staging plan does not match the saved restore intent. Discard it before continuing.')
      }
      return
    }

    if (!resumeMatchesBackup) return
    if (restored.status === 'abandoned') {
      onStagingResumeChange(null)
      setPlan(null)
      setValidated(null)
      setNotice('The saved staging plan was already abandoned on the server. Its browser reference was cleared.')
      setError('')
      return
    }
    setMode(restored.jwtSecretMode)
    setPlan(restored)
    setValidated(
      restored.status === 'validated' && restored.activationPerformed !== true
        ? restored
        : null,
    )
    if (!isCanonicalStagingStatus(restored.status)) {
      setError('The server returned an unknown staging status. Restore remains blocked.')
    } else if (restored.status === 'failed' || restored.status === 'quarantined') {
      setError(stagingFailureMessage(restored))
    } else if (restored.activationPerformed === true) {
      setError('This staging plan was already activated and cannot be submitted again.')
    } else {
      setError('')
    }
  }, [
    resumeMatchesBackup,
    onStagingResumeChange,
    resumedPlan.data,
    resumedPlan.error,
    resumedPlan.isError,
    stagingResume,
  ])

  const filename = resumeMatchesBackup
    ? stagingResume.filename
    : inspection?.filename || backup?.filename || backup?.key || id || ''
  const trusted = isTrusted(inspection?.trustStatus, inspection?.authenticated)
  const invalid = inspection?.integrityStatus === 'invalid' || inspection?.status === 'invalid'
  const busy = approveSigner.isPending
    || createPlan.isPending
    || executePlan.isPending
    || abandonPlan.isPending
    || activatePlan.isPending
  const reloadingResume = resumeMatchesBackup && resumedPlan.isLoading
  const resumeUnavailable = resumeMatchesBackup && resumedPlan.isError && !resumedPlan.data

  const stageDescription = useMemo(() => {
    if (reloadingResume) return 'Reloading and verifying the saved staging plan from the server.'
    if (resumeUnavailable) return 'The saved staging plan could not be verified. Activation remains blocked.'
    if (!plan) return 'No staging target has been created yet.'
    if (plan.status === 'planned') return 'The plan is saved and ready to restore into isolated targets.'
    if (plan.status === 'running') return 'Restore staging is running on the server. This page polls its canonical status.'
    if (plan.status === 'validated') return 'The new database and data directory passed staging validation.'
    if (plan.status === 'failed' || plan.status === 'quarantined') return stagingFailureMessage(plan)
    if (plan.status === 'abandoned') return 'The staging plan was abandoned and cannot be resumed.'
    return `Plan ${plan.id} returned an unknown canonical status.`
  }, [plan, reloadingResume, resumeUnavailable])

  const handleApprove = async () => {
    if (!id || !inspection?.signerFingerprintSha256) return
    setError('')
    try {
      await approveSigner.mutateAsync({
        id,
        fingerprintSha256: inspection.signerFingerprintSha256,
      })
      await refetch()
      toast.success('Backup signer approved.')
    } catch (err) {
      setError(getBackupErrorMessage(err, 'The backup signer could not be approved.'))
    }
  }

  const applyExecutionResult = (
    result: BackupStagingPlan,
    resume: StagingResumeState,
  ): boolean => {
    if (!stagingPlanMatchesResume(result, resume) || !isCanonicalStagingStatus(result.status)) {
      setError('The server returned a staging result that does not match the saved restore intent.')
      return false
    }
    setPlan(result)
    setValidated(
      result.status === 'validated' && result.activationPerformed !== true
        ? result
        : null,
    )
    if (result.status === 'validated' && result.activationPerformed !== true) {
      setError('')
      toast.success('Restore staging completed and passed canonical validation.')
    } else if (result.status === 'failed' || result.status === 'quarantined') {
      setError(stagingFailureMessage(result))
    } else if (result.status === 'abandoned') {
      onStagingResumeChange(null)
      setPlan(null)
      setError('The staging plan was abandoned before execution completed.')
    } else if (result.activationPerformed === true) {
      setError('This staging plan was already activated and cannot be submitted again.')
    } else {
      setError('')
    }
    return true
  }

  const executeSavedPlan = async (
    selectedPlan: BackupStagingPlan,
    resume: StagingResumeState,
  ) => {
    if (
      selectedPlan.status !== 'planned'
      || !stagingPlanMatchesResume(selectedPlan, resume)
      || executePlan.isPending
    ) return
    setError('')
    setNotice('')
    setConfirmation('')
    try {
      const result = await executePlan.mutateAsync({
        planId: selectedPlan.id,
        planHash: selectedPlan.planHash,
      })
      applyExecutionResult(result, resume)
    } catch (err) {
      const message = isExplicitHttpError(err)
        ? getBackupErrorMessage(err, 'Restore staging could not be executed. The saved plan remains available.')
        : 'The staging connection ended without a definitive response. The saved plan is retained and its canonical status will be checked.'
      setError(message)
    }
  }

  const handleStage = async () => {
    if (!id || !trusted || invalid || stagingResume) return
    setError('')
    setNotice('')
    setPlan(null)
    setValidated(null)
    setConfirmation('')
    let created: BackupStagingPlan
    try {
      created = await createPlan.mutateAsync({ id, jwtSecretMode: mode })
    } catch (err) {
      setError(getBackupErrorMessage(err, 'The restore staging plan could not be created.'))
      return
    }
    if (
      !created.id
      || !created.planHash
      || created.backupId !== id
      || created.jwtSecretMode !== mode
      || created.status !== 'planned'
    ) {
      setError('The server returned an invalid newly-created staging plan. It was not executed.')
      return
    }

    const resume: StagingResumeState = {
      planId: created.id,
      planHash: created.planHash,
      backupId: created.backupId,
      mode: created.jwtSecretMode,
      filename,
    }
    setPlan(created)
    let persisted: boolean
    try {
      persisted = onStagingResumeChange(resume)
    } catch {
      setError('The browser could not retain the new staging plan, so it was not executed.')
      return
    }
    if (!persisted) {
      const message = 'The staging plan is saved on the server, but this browser cannot retain its reference across a reload. Keep this page open.'
      setNotice(message)
      toast.warning(message)
    }
    await executeSavedPlan(created, resume)
  }

  const handleActivate = async () => {
    const selectedPlan = validated
    if (
      !id
      || !selectedPlan
      || !stagingResume
      || !stagingPlanMatchesResume(selectedPlan, stagingResume)
      || stagingResume.backupId !== id
      || selectedPlan.status !== 'validated'
      || selectedPlan.activationPerformed === true
      || confirmation !== filename
    ) return
    setError('')
    setNotice('')
    let intent: ActivationResumeState
    try {
      intent = createActivationResume({
        planId: selectedPlan.id,
        planHash: selectedPlan.planHash,
        mode: selectedPlan.jwtSecretMode,
        backupId: id,
        filename,
      })
    } catch (err) {
      setError(getBackupErrorMessage(err, 'Secure browser randomness is required before activation.'))
      return
    }

    let persisted: boolean
    try {
      persisted = onActivationResumeChange(intent)
    } catch {
      setError('The activation monitor could not be initialized, so activation was not submitted.')
      return
    }
    if (!persisted) {
      const message = 'This browser cannot persist restart tracking. Keep this page open until activation finishes.'
      setNotice(message)
      toast.warning(message)
    }

    let started
    try {
      started = await activatePlan.mutateAsync({
        planId: selectedPlan.id,
        planHash: selectedPlan.planHash,
        activationId: intent.activationId,
        resumeToken: intent.resumeToken,
      })
    } catch (err) {
      if (isExplicitHttpError(err)) {
        onActivationResumeChange(null)
        setError(getBackupErrorMessage(err, 'Activation was rejected. The validated staging plan remains available.'))
        return
      }
      const message = 'The activation request ended without a definitive HTTP response. PPBase will be monitored with the pre-registered activation credentials; do not retry while its status is unknown.'
      setNotice(message)
      toast.warning(message)
      onOpenChange(false)
      return
    }

    if (!activationStartMatchesResume(started, intent)) {
      const message = 'Activation returned credentials that do not match the saved intent. Monitoring remains locked to the original credentials until the server state is known.'
      setNotice(message)
      toast.warning(message)
      onOpenChange(false)
      return
    }

    onActivationAccepted()
    if (persisted) {
      toast.success('Activation accepted. PPBase may restart while the health gate runs.')
    } else {
      toast.warning('Activation was accepted, but this browser cannot persist restart tracking. Keep this page open; reloading will lose the activation monitor.')
    }
    onOpenChange(false)
  }

  const handleDiscard = async () => {
    if (!stagingResume || plan?.status === 'running' || abandonPlan.isPending) return
    setError('')
    setNotice('')
    try {
      await abandonPlan.mutateAsync({
        planId: stagingResume.planId,
        planHash: stagingResume.planHash,
      })
    } catch (err) {
      if (errorStatus(err) !== 404) {
        setError(getBackupErrorMessage(err, 'The staging plan could not be abandoned safely.'))
        return
      }
    }
    onStagingResumeChange(null)
    setPlan(null)
    setValidated(null)
    setConfirmation('')
    toast.success('Restore staging plan abandoned.')
  }

  const copyFingerprint = async () => {
    const value = inspection?.signerFingerprintSha256
    if (!value) return
    await navigator.clipboard.writeText(value)
    toast.success('Fingerprint copied.')
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !busy && onOpenChange(next)}>
      <DialogContent className="max-h-[92vh] max-w-3xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Restore {filename || 'backup'}</DialogTitle>
          <DialogDescription>
            Restore into isolated targets first, validate them, then explicitly activate them.
          </DialogDescription>
        </DialogHeader>

        {isLoading ? (
          <div className="flex min-h-48 items-center justify-center">
            <LoadingSpinner size="lg" />
          </div>
        ) : isError || !inspection ? (
          <div className="flex min-h-48 flex-col items-center justify-center gap-3 text-center">
            <AlertTriangle className="h-9 w-9 text-red-500" />
            <div>
              <p className="font-semibold">The backup could not be inspected.</p>
              <p className="mt-1 text-sm text-slate-500">
                Restore remains blocked until integrity and provenance can be verified.
              </p>
            </div>
            <Button type="button" variant="outline" onClick={() => refetch()}>
              Retry inspection
            </Button>
          </div>
        ) : (
          <div className="space-y-5">
            <div className="rounded-xl border bg-slate-50 p-4">
              <div className="mb-3 flex flex-wrap items-center gap-2">
                <ShieldCheck className="h-5 w-5 text-indigo-600" />
                <span className="font-semibold">Integrity and provenance</span>
                <Badge variant={trusted ? 'secondary' : 'destructive'}>
                  {trusted ? 'Trusted signer' : 'Approval required'}
                </Badge>
                <Badge variant={invalid ? 'destructive' : 'outline'}>
                  {inspection.integrityStatus || (inspection.resourcesVerified ? 'valid' : 'unchecked')}
                </Badge>
              </div>
              <div className="flex items-start gap-2 text-xs text-slate-600">
                <code className="min-w-0 flex-1 break-all rounded bg-white px-2 py-1.5">
                  {inspection.signerFingerprintSha256 || 'No signer fingerprint'}
                </code>
                {inspection.signerFingerprintSha256 && (
                  <Button variant="ghost" size="icon" onClick={copyFingerprint} aria-label="Copy fingerprint">
                    <Copy className="h-4 w-4" />
                  </Button>
                )}
              </div>

              {!trusted && !invalid && inspection.signerFingerprintSha256 && (
                <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50 p-3">
                  <p className="mb-3 text-sm text-amber-900">
                    The signature is valid, but this external Ed25519 identity is not approved on this server.
                    Staging and activation remain blocked.
                  </p>
                  <Button
                    type="button"
                    onClick={handleApprove}
                    disabled={approveSigner.isPending}
                  >
                    {approveSigner.isPending && <LoadingSpinner size="sm" />}
                    Approve this exact Ed25519 key
                  </Button>
                </div>
              )}
            </div>

            <div>
              <div className="mb-2 flex items-center gap-2">
                <Database className="h-5 w-5 text-indigo-600" />
                <h3 className="font-semibold">1. Select session mode</h3>
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                {(['disaster_recovery', 'clone'] as JwtSecretMode[]).map((value) => (
                  <button
                    type="button"
                    key={value}
                    disabled={!!stagingResume || !!plan || busy}
                    onClick={() => setMode(value)}
                    className={`rounded-xl border p-4 text-left transition-colors ${
                      mode === value ? 'border-indigo-500 bg-indigo-50' : 'hover:bg-slate-50'
                    }`}
                  >
                    <div className="mb-1 font-semibold">{modeLabel(value)}</div>
                    <p className="text-sm text-slate-600">
                      {value === 'disaster_recovery'
                        ? 'Keep the signed JWT secret and preserve compatible sessions from the backup.'
                        : 'Generate a new JWT secret and invalidate every previous session and purpose token.'}
                    </p>
                  </button>
                ))}
              </div>
            </div>

            <div className="rounded-xl border p-4">
              <div className="mb-2 flex items-center justify-between gap-3">
                <div>
                  <h3 className="font-semibold">2. Stage, migrate and validate</h3>
                  <p className="text-sm text-slate-600">{stageDescription}</p>
                </div>
                {validated?.status === 'validated' && <CheckCircle2 className="h-6 w-6 text-emerald-600" />}
              </div>
              {!!plan?.preflightWarnings?.length && (
                <div className="mb-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
                  <div className="mb-1 font-medium">Restore preflight warnings</div>
                  <ul className="list-disc space-y-1 pl-5">
                    {plan.preflightWarnings.map((warning) => <li key={warning}>{warning}</li>)}
                  </ul>
                </div>
              )}
              {!stagingResume && (
                <Button
                  type="button"
                  onClick={handleStage}
                  disabled={!trusted || invalid || createPlan.isPending || executePlan.isPending}
                >
                  {(createPlan.isPending || executePlan.isPending) && <LoadingSpinner size="sm" />}
                  {executePlan.isPending ? 'Restoring and validating…' : 'Create and execute staging plan'}
                </Button>
              )}
              {resumeMatchesBackup && (
                <div className="flex flex-wrap items-center gap-2">
                  {reloadingResume ? (
                    <div className="flex items-center gap-2 text-sm text-slate-600">
                      <LoadingSpinner size="sm" /> Verifying saved plan…
                    </div>
                  ) : (
                    <>
                      {resumeUnavailable && (
                        <Button type="button" variant="outline" onClick={() => resumedPlan.refetch()}>
                          Retry saved plan
                        </Button>
                      )}
                      {plan?.status === 'planned' && (
                        <Button
                          type="button"
                          onClick={() => stagingResume && executeSavedPlan(plan, stagingResume)}
                          disabled={executePlan.isPending}
                        >
                          {executePlan.isPending && <LoadingSpinner size="sm" />}
                          Execute saved staging plan
                        </Button>
                      )}
                      {plan?.status === 'running' && (
                        <div className="flex items-center gap-2 text-sm text-slate-600">
                          <LoadingSpinner size="sm" /> Restoring and validating on the server…
                        </div>
                      )}
                      <Button
                        type="button"
                        variant="ghost"
                        onClick={handleDiscard}
                        disabled={plan?.status === 'running' || abandonPlan.isPending}
                      >
                        {abandonPlan.isPending && <LoadingSpinner size="sm" />}
                        Abandon saved plan
                      </Button>
                    </>
                  )}
                </div>
              )}
              {validated?.validation && (
                <pre className="mt-3 max-h-48 overflow-auto rounded-lg bg-slate-950 p-3 text-xs text-slate-100">
                  {JSON.stringify(validated.validation, null, 2)}
                </pre>
              )}
              {!!validated?.migrationsApplied?.length && (
                <p className="mt-3 text-sm text-slate-600">
                  Applied missing migrations: {validated.migrationsApplied.join(', ')}
                </p>
              )}
            </div>

            {validated?.status === 'validated' && (
              <div className="rounded-xl border border-red-200 bg-red-50 p-4">
                <div className="mb-2 flex items-center gap-2 text-red-800">
                  <AlertTriangle className="h-5 w-5" />
                  <h3 className="font-semibold">3. Confirm activation</h3>
                </div>
                <p className="mb-3 text-sm text-red-800">
                  PPBase will switch to the validated database and data directory, restart, run its health gate,
                  and roll back automatically if the new target does not become healthy. The old targets are kept.
                </p>
                <Label htmlFor="backup-activation-confirmation">
                  Type <code className="rounded bg-white px-1.5 py-0.5">{filename}</code> to confirm
                </Label>
                <Input
                  id="backup-activation-confirmation"
                  className="mt-2"
                  value={confirmation}
                  onChange={(event) => setConfirmation(event.target.value)}
                  autoComplete="off"
                  disabled={activatePlan.isPending}
                />
                <Button
                  type="button"
                  variant="destructive"
                  className="mt-3"
                  onClick={handleActivate}
                  disabled={confirmation !== filename || activatePlan.isPending}
                >
                  {activatePlan.isPending ? <LoadingSpinner size="sm" /> : <RefreshCw className="h-4 w-4" />}
                  Activate and restart PPBase
                </Button>
              </div>
            )}

            {error && (
              <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">
                {error}
              </div>
            )}
            {notice && (
              <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
                {notice}
              </div>
            )}
          </div>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={busy}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
