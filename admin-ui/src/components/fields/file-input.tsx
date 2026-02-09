import { useRef, useState, useEffect } from 'react'
import { Label } from '@/components/ui/label'
import { Button } from '@/components/ui/button'
import { Upload, X, FileIcon, ImageIcon } from 'lucide-react'
import type { FieldInputProps } from '@/components/record-field-input'
import { ImagePreview } from '@/components/ui/image-preview'

interface FileData {
  name: string
  isNew?: boolean
  file?: File
}

export function FileInput({ field, value, onChange, collectionId, recordId }: FieldInputProps) {
  const inputRef = useRef<HTMLInputElement>(null)
  const maxSelect = field.options?.maxSelect ?? field.maxSelect ?? 1
  const isMultiple = maxSelect > 1
  const maxSize = field.options?.maxSize ?? field.maxSize
  const mimeTypes = field.options?.mimeTypes ?? field.mimeTypes ?? []

  // Parse current value into file list
  const parseValue = (): FileData[] => {
    if (!value) return []
    if (Array.isArray(value)) {
      return value.map((v) => {
        if (typeof v === 'string') return { name: v }
        if (v instanceof File) return { name: v.name, isNew: true, file: v }
        return { name: String(v) }
      })
    }
    if (typeof value === 'string') return [{ name: value }]
    return []
  }

  const [files, setFiles] = useState<FileData[]>(parseValue)
  const [pendingFiles, setPendingFiles] = useState<File[]>([])

  useEffect(() => {
    setFiles(parseValue())
  }, [value])

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const selected = Array.from(e.target.files || [])
    if (!selected.length) return

    // Validate file size
    if (maxSize) {
      for (const file of selected) {
        if (file.size > maxSize) {
          alert(`File "${file.name}" exceeds max size of ${Math.round(maxSize / 1024)}KB`)
          return
        }
      }
    }

    // Validate MIME types
    if (mimeTypes.length > 0) {
      for (const file of selected) {
        const isAllowed = mimeTypes.some((mime: string) => {
          if (mime.endsWith('/*')) {
            return file.type.startsWith(mime.slice(0, -1))
          }
          return file.type === mime
        })
        if (!isAllowed) {
          alert(`File "${file.name}" has unsupported type: ${file.type}`)
          return
        }
      }
    }

    if (isMultiple) {
      // Limit to maxSelect
      const remaining = maxSelect - files.length
      if (remaining <= 0) {
        alert(`Maximum of ${maxSelect} files allowed.`)
        return
      }
      const toAdd = selected.slice(0, remaining)
      if (toAdd.length < selected.length) {
        alert(`Only ${remaining} more file(s) allowed. ${selected.length - toAdd.length} file(s) were ignored.`)
      }

      const newFiles = [...pendingFiles, ...toAdd]
      const allFileData = [
        ...files,
        ...toAdd.map((f) => ({ name: f.name, isNew: true, file: f })),
      ]
      setPendingFiles(newFiles)
      setFiles(allFileData)
      onChange(allFileData.map((f) => f.file || f.name))
    } else {
      // Replace single file
      const file = selected[0]
      const fileData = [{ name: file.name, isNew: true, file }]
      setPendingFiles([file])
      setFiles(fileData)
      onChange([file])
    }

    // Reset input
    if (inputRef.current) inputRef.current.value = ''
  }

  const handleRemove = (index: number) => {
    const removed = files[index]
    console.log('[FileInput] handleRemove:', { removed, isNew: removed.isNew })
    const newFiles = files.filter((_, i) => i !== index)
    setFiles(newFiles)

    if (removed.isNew) {
      // Remove from pending files
      const newPending = pendingFiles.filter((f) => f.name !== removed.name)
      setPendingFiles(newPending)
    }

    // Update value - send empty string when all files removed so backend can delete them
    const newValue = newFiles.map((f) => f.file || f.name)
    const finalValue = newValue.length > 0 ? newValue : ''
    console.log('[FileInput] onChange called with:', finalValue)
    onChange(finalValue)
  }

  const isImage = (filename: string) => {
    return /\.(jpg|jpeg|png|gif|webp|svg)$/i.test(filename)
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <Label>{field.name}</Label>
        {field.required && <span className="text-destructive text-sm">*</span>}
      </div>

      {/* File list */}
      {files.length > 0 && (
        <div className="space-y-2">
          {files.map((file, index) => (
            <div
              key={`${file.name}-${index}`}
              className="flex items-center gap-3 p-2 bg-muted rounded-md group relative"
            >
              {/* Preview Thumbnail */}
              <div className="h-10 w-10 shrink-0 overflow-hidden rounded bg-background border flex items-center justify-center relative">
                {file.isNew && file.file && isImage(file.name) ? (
                  <img
                    src={URL.createObjectURL(file.file)}
                    alt={file.name}
                    className="h-full w-full object-cover"
                    onLoad={(e) => URL.revokeObjectURL(e.currentTarget.src)}
                  />
                ) : !file.isNew && collectionId && recordId && isImage(file.name) ? (
                  <div className="absolute inset-0 p-0.5">
                    <ImagePreview
                      collectionId={collectionId}
                      recordId={recordId}
                      files={file.name}
                      className="h-full w-full"
                      size="fill"
                    />
                  </div>
                ) : isImage(file.name) ? (
                  <ImageIcon className="h-5 w-5 text-muted-foreground" />
                ) : (
                  <FileIcon className="h-5 w-5 text-muted-foreground" />
                )}
              </div>

              <span className="text-sm truncate flex-1">{file.name}</span>

              {file.isNew && (
                <span className="text-xs text-blue-600 bg-blue-100 px-1.5 py-0.5 rounded">
                  New
                </span>
              )}

              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-8 w-8 p-0 text-muted-foreground hover:text-destructive"
                onClick={() => handleRemove(index)}
              >
                <X className="h-4 w-4" />
              </Button>
            </div>
          ))}
        </div>
      )}

      {/* Upload button */}
      <div>
        <input
          ref={inputRef}
          type="file"
          multiple={isMultiple}
          accept={mimeTypes.length > 0 ? mimeTypes.join(',') : undefined}
          className="hidden"
          onChange={handleFileSelect}
        />
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => inputRef.current?.click()}
        >
          <Upload className="h-4 w-4 mr-2" />
          {isMultiple
            ? 'Add files'
            : files.length > 0
              ? 'Change file'
              : 'Choose file'}
        </Button>
      </div>

      {/* Hints */}
      <div className="text-xs text-muted-foreground space-y-0.5">
        {maxSize && (
          <p>Max size: {maxSize >= 1024 * 1024 ? `${Math.round(maxSize / 1024 / 1024)}MB` : `${Math.round(maxSize / 1024)}KB`}</p>
        )}
        {mimeTypes.length > 0 && (
          <p>Allowed types: {mimeTypes.join(', ')}</p>
        )}
        {isMultiple && (
          <p>Max files: {maxSelect}</p>
        )}
      </div>
    </div>
  )
}
