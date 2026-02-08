import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { MoreVertical } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
  SheetFooter,
} from '@/components/ui/sheet'
import { LoadingSpinner } from '@/components/loading-spinner'
import { RecordFieldInput } from '@/components/record-field-input'
import { RecordActionsMenu } from '@/components/record-actions-menu'
import { ConfirmDialog } from '@/components/confirm-dialog'
import { useRecord, useCreateRecord, useUpdateRecord, useDeleteRecord } from '@/hooks/use-records'
import { useCollections } from '@/hooks/use-collections'
import { changeAdminPassword } from '@/api/endpoints/admins'
import type { Collection, Field, RecordModel } from '@/api/types'

interface RecordEditorProps {
  open: boolean
  onClose: () => void
  collection: Collection
  recordId?: string | null
  duplicateData?: Record<string, unknown> | null
}

function getFields(collection: Collection): Field[] {
  return collection.fields ?? collection.schema ?? []
}

export function RecordEditor({
  open,
  onClose,
  collection,
  recordId,
  duplicateData,
}: RecordEditorProps) {
  const navigate = useNavigate()
  const isEditing = !!recordId
  const fields = getFields(collection)
  const { data: collections } = useCollections()
  const { data: existingRecord, isLoading: isRecordLoading } = useRecord(
    collection.id,
    recordId ?? undefined,
  )

  const createMutation = useCreateRecord(collection.id)
  const updateMutation = useUpdateRecord(collection.id)
  const deleteMutation = useDeleteRecord(collection.id)

  const [formData, setFormData] = useState<Record<string, unknown>>({})
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false)

  // Password change state for _superusers collection
  const isSuperusersCollection = collection.name === '_superusers'
  const [changePassword, setChangePassword] = useState(false)
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [isChangingPassword, setIsChangingPassword] = useState(false)

  // Initialize form data
  useEffect(() => {
    if (duplicateData) {
      setFormData({ ...duplicateData })
    } else if (isEditing && existingRecord) {
      const data: Record<string, unknown> = {}
      for (const field of fields) {
        data[field.name] = existingRecord[field.name] ?? null
      }
      setFormData(data)
    } else if (!isEditing) {
      const data: Record<string, unknown> = {}
      for (const field of fields) {
        if (field.type === 'bool') {
          data[field.name] = false
        } else {
          data[field.name] = null
        }
      }
      setFormData(data)
    }
  }, [existingRecord, duplicateData, isEditing, fields])

  const handleFieldChange = (fieldName: string, value: unknown) => {
    setFormData((prev) => ({ ...prev, [fieldName]: value }))
  }

  const handleSave = async () => {
    try {
      if (isEditing) {
        await updateMutation.mutateAsync({ id: recordId!, data: formData })
        toast.success('Record updated successfully')
      } else {
        await createMutation.mutateAsync(formData)
        toast.success('Record created successfully')
      }
      onClose()
    } catch (err: unknown) {
      const message =
        err && typeof err === 'object' && 'message' in err
          ? String((err as { message: string }).message)
          : 'Failed to save record'
      toast.error(message)
    }
  }

  const handlePasswordChange = async () => {
    if (!recordId) return
    if (newPassword !== confirmPassword) {
      toast.error('Passwords do not match')
      return
    }
    if (newPassword.length < 8) {
      toast.error('Password must be at least 8 characters')
      return
    }

    setIsChangingPassword(true)
    try {
      await changeAdminPassword(recordId, {
        password: newPassword,
        passwordConfirm: confirmPassword,
      })
      toast.success('Password changed successfully. Please log in again.')
      // Clear auth and redirect to login
      localStorage.removeItem('ppbase_token')
      localStorage.removeItem('ppbase_admin')
      onClose()
      navigate('/login')
    } catch (err: unknown) {
      const message =
        err && typeof err === 'object' && 'message' in err
          ? String((err as { message: string }).message)
          : 'Failed to change password'
      toast.error(message)
    } finally {
      setIsChangingPassword(false)
    }
  }

  const handleDelete = async () => {
    if (!recordId) return
    try {
      await deleteMutation.mutateAsync(recordId)
      toast.success('Record deleted successfully')
      setShowDeleteConfirm(false)
      onClose()
    } catch (err: unknown) {
      const message =
        err && typeof err === 'object' && 'message' in err
          ? String((err as { message: string }).message)
          : 'Failed to delete record'
      toast.error(message)
    }
  }

  const handleDuplicate = () => {
    if (!existingRecord) return
    const data: Record<string, unknown> = {}
    for (const field of fields) {
      data[field.name] = existingRecord[field.name] ?? null
    }
    onClose()
    // Signal parent to open with duplicate data
    setTimeout(() => {
      window.dispatchEvent(
        new CustomEvent('ppbase:duplicate-record', { detail: data }),
      )
    }, 200)
  }

  const isSaving = createMutation.isPending || updateMutation.isPending
  const title = isEditing
    ? `Edit ${collection.name} record`
    : `New ${collection.name} record`

  return (
    <>
      <Sheet open={open} onOpenChange={(o) => !o && onClose()}>
        <SheetContent className="sm:max-w-[700px] flex flex-col overflow-hidden">
          <SheetHeader className="shrink-0 px-6 pt-6 pb-4">
            <div className="flex items-center justify-between pr-8">
              <SheetTitle>{title}</SheetTitle>
              {isEditing && existingRecord && (
                <RecordActionsMenu
                  record={existingRecord}
                  trigger={
                    <Button variant="ghost" size="icon" className="h-8 w-8">
                      <MoreVertical className="h-4 w-4" />
                    </Button>
                  }
                  onDuplicate={handleDuplicate}
                  onDelete={() => setShowDeleteConfirm(true)}
                />
              )}
            </div>
            <SheetDescription>
              {isEditing && recordId && (
                <span className="font-mono text-xs">ID: {recordId}</span>
              )}
            </SheetDescription>
          </SheetHeader>

          <div className="border-t shrink-0" />

          <div className="flex-1 overflow-y-auto px-6">
            {isEditing && isRecordLoading ? (
              <div className="flex justify-center py-12">
                <LoadingSpinner />
              </div>
            ) : (
              <div className="space-y-4 py-4">
                {fields.map((field) => (
                  <RecordFieldInput
                    key={field.name}
                    field={field}
                    value={formData[field.name]}
                    onChange={(value) => handleFieldChange(field.name, value)}
                    collections={collections}
                  />
                ))}
                {fields.length === 0 && (
                  <p className="text-sm text-muted-foreground py-4">
                    This collection has no editable fields.
                  </p>
                )}

                {/* Password change section for _superusers */}
                {isSuperusersCollection && isEditing && (
                  <div className="border-t pt-4 mt-4 space-y-4">
                    <div className="flex items-center gap-3">
                      <Checkbox
                        id="change-password-toggle"
                        checked={changePassword}
                        onCheckedChange={(checked) => {
                          setChangePassword(!!checked)
                          if (!checked) {
                            setNewPassword('')
                            setConfirmPassword('')
                          }
                        }}
                      />
                      <Label htmlFor="change-password-toggle" className="cursor-pointer font-medium">
                        Change password
                      </Label>
                    </div>

                    {changePassword && (
                      <div className="flex gap-4">
                        <div className="flex-1 space-y-1.5">
                          <Label htmlFor="new-password">New Password *</Label>
                          <Input
                            id="new-password"
                            type="password"
                            placeholder="Min. 8 characters"
                            value={newPassword}
                            onChange={(e) => setNewPassword(e.target.value)}
                          />
                        </div>
                        <div className="flex-1 space-y-1.5">
                          <Label htmlFor="confirm-password">Confirm Password *</Label>
                          <Input
                            id="confirm-password"
                            type="password"
                            placeholder="Confirm password"
                            value={confirmPassword}
                            onChange={(e) => setConfirmPassword(e.target.value)}
                          />
                        </div>
                      </div>
                    )}

                    {changePassword && (
                      <Button
                        onClick={handlePasswordChange}
                        disabled={isChangingPassword || !newPassword || !confirmPassword}
                        variant="destructive"
                      >
                        {isChangingPassword && <LoadingSpinner size="sm" className="mr-2" />}
                        Change Password
                      </Button>
                    )}
                  </div>
                )}
              </div>
            )}
          </div>

          <div className="border-t shrink-0" />

          <SheetFooter className="shrink-0 px-6 py-2.5">
            <Button variant="outline" onClick={onClose} disabled={isSaving}>
              Cancel
            </Button>
            <Button onClick={handleSave} disabled={isSaving || fields.length === 0}>
              {isSaving && <LoadingSpinner size="sm" className="mr-2" />}
              {isEditing ? 'Save changes' : 'Create'}
            </Button>
          </SheetFooter>
        </SheetContent>
      </Sheet>

      <ConfirmDialog
        open={showDeleteConfirm}
        onOpenChange={setShowDeleteConfirm}
        title="Delete record"
        description="Are you sure you want to delete this record? This action cannot be undone."
        confirmLabel="Delete"
        variant="destructive"
        onConfirm={handleDelete}
      />
    </>
  )
}
