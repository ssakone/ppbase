import { useRef, useState, useEffect, useCallback } from 'react'
import { Label } from '@/components/ui/label'
import { Button } from '@/components/ui/button'
import { Upload, X, FileIcon, ImageIcon, Plus } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { FieldInputProps } from '@/components/record-field-input'
import { ImagePreview } from '@/components/ui/image-preview'

interface FileData {
  name: string
  isNew?: boolean
  file?: File
}

function formatBytes(size: number) {
  if (size >= 1024 * 1024) return `${Math.round((size / 1024 / 1024) * 10) / 10}MB`
  if (size >= 1024) return `${Math.round(size / 1024)}KB`
  return `${size}B`
}

export function FileInput({ field, value, onChange, collectionId, recordId }: FieldInputProps) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [isDragActive, setIsDragActive] = useState(false)

  const maxSelect = field.options?.maxSelect ?? field.maxSelect ?? 1
  const isMultiple = maxSelect > 1
  const maxSize = field.options?.maxSize ?? field.maxSize
  const mimeTypes = field.options?.mimeTypes ?? field.mimeTypes ?? []

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

  useEffect(() => {
    setFiles(parseValue())
  }, [value])

  const isImage = (filename: string) => {
    return /\.(jpg|jpeg|png|gif|webp|svg)$/i.test(filename)
  }

  const applySelectedFiles = useCallback(
    (selected: File[]) => {
      if (!selected.length) return

      if (maxSize) {
        for (const file of selected) {
          if (file.size > maxSize) {
            alert(`File "${file.name}" exceeds max size of ${Math.round(maxSize / 1024)}KB`)
            return
          }
        }
      }

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
        const remaining = maxSelect - files.length
        if (remaining <= 0) {
          alert(`Maximum of ${maxSelect} files allowed.`)
          return
        }

        const toAdd = selected.slice(0, remaining)
        if (toAdd.length < selected.length) {
          alert(`Only ${remaining} more file(s) allowed. ${selected.length - toAdd.length} file(s) were ignored.`)
        }

        const nextFiles = [
          ...files,
          ...toAdd.map((f) => ({ name: f.name, isNew: true, file: f })),
        ]

        setFiles(nextFiles)
        onChange(nextFiles.map((f) => f.file || f.name))
      } else {
        const file = selected[0]
        const nextFiles = [{ name: file.name, isNew: true, file }]
        setFiles(nextFiles)
        onChange([file])
      }

      if (inputRef.current) inputRef.current.value = ''
    },
    [files, isMultiple, maxSelect, maxSize, mimeTypes, onChange],
  )

  const handleInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    applySelectedFiles(Array.from(e.target.files || []))
  }

  const handleDrop = (e: React.DragEvent<HTMLButtonElement>) => {
    e.preventDefault()
    setIsDragActive(false)
    applySelectedFiles(Array.from(e.dataTransfer.files || []))
  }

  const handleRemove = (index: number) => {
    const nextFiles = files.filter((_, i) => i !== index)
    setFiles(nextFiles)

    const nextValue = nextFiles.map((f) => f.file || f.name)
    onChange(nextValue.length > 0 ? nextValue : '')
  }

  const actionLabel = isMultiple
    ? 'Add files'
    : files.length > 0
      ? 'Change file'
      : 'Choose file'

  const infoParts: string[] = []
  if (maxSize) infoParts.push(`Max ${formatBytes(maxSize)}`)
  if (mimeTypes.length > 0) infoParts.push(mimeTypes.join(', '))
  if (isMultiple) infoParts.push(`Up to ${maxSelect} files`)

  return (
    <div className="space-y-2.5">
      <div className="flex items-center gap-2">
        <Label>{field.name}</Label>
        {field.required && <span className="text-destructive text-sm">*</span>}
      </div>

      <input
        ref={inputRef}
        type="file"
        multiple={isMultiple}
        accept={mimeTypes.length > 0 ? mimeTypes.join(',') : undefined}
        className="hidden"
        onChange={handleInputChange}
      />

      <button
        type="button"
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault()
          setIsDragActive(true)
        }}
        onDragLeave={(e) => {
          e.preventDefault()
          setIsDragActive(false)
        }}
        onDrop={handleDrop}
        className={cn(
          'w-full rounded-xl border bg-slate-100/80 px-4 py-4 text-left transition-colors',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          isDragActive
            ? 'border-indigo-400 bg-indigo-50/70'
            : 'border-slate-300 hover:border-slate-400 hover:bg-slate-100',
        )}
      >
        <div className="flex items-center gap-3">
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-slate-300 bg-white text-slate-500">
            <Upload className="h-4 w-4" />
          </span>

          <div className="min-w-0">
            <p className="text-sm font-medium text-slate-800">
              {isMultiple ? 'Drop files here or click to browse' : 'Drop a file here or click to browse'}
            </p>
            {infoParts.length > 0 && (
              <p className="mt-0.5 truncate text-xs text-slate-500">{infoParts.join(' • ')}</p>
            )}
          </div>

          <span className="ml-auto inline-flex h-8 shrink-0 items-center gap-1 rounded-md border border-slate-300 bg-white px-2.5 text-xs font-medium text-slate-700">
            <Plus className="h-3.5 w-3.5" />
            {actionLabel}
          </span>
        </div>
      </button>

      {files.length > 0 && (
        <div className="space-y-2">
          {files.map((file, index) => (
            <div
              key={`${file.name}-${index}`}
              className="flex items-center gap-3 rounded-lg border border-slate-200 bg-white p-2.5"
            >
              <div className="relative h-12 w-12 shrink-0 overflow-hidden rounded-md border bg-slate-50">
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
                  <div className="flex h-full w-full items-center justify-center text-slate-400">
                    <ImageIcon className="h-4 w-4" />
                  </div>
                ) : (
                  <div className="flex h-full w-full items-center justify-center text-slate-400">
                    <FileIcon className="h-4 w-4" />
                  </div>
                )}
              </div>

              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium text-slate-800">{file.name}</p>
                <p className="text-xs text-slate-500">{file.isNew ? 'New file' : 'Existing file'}</p>
              </div>

              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-8 w-8 p-0 text-slate-500 hover:text-destructive"
                onClick={() => handleRemove(index)}
              >
                <X className="h-4 w-4" />
              </Button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
