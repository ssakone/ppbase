import { useState, useEffect } from 'react'
import { FileIcon, ImageIcon, X, ChevronLeft, ChevronRight, Download, ExternalLink } from 'lucide-react'
import {
    Dialog,
    DialogContent,
    DialogClose,
    DialogTitle,
    DialogDescription,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { apiClient } from '@/api/client'
import { cn } from '@/lib/utils'

let cachedFileToken = ''
let cachedFileTokenExpiresAt = 0
let pendingFileToken: Promise<string> | null = null

async function getFileToken(): Promise<string> {
    const now = Date.now()
    if (cachedFileToken && cachedFileTokenExpiresAt > now) {
        return cachedFileToken
    }
    if (pendingFileToken) {
        return pendingFileToken
    }

    pendingFileToken = apiClient
        .request<{ token: string }>('POST', '/api/files/token')
        .then((response) => {
            cachedFileToken = response.token || ''
            cachedFileTokenExpiresAt = Date.now() + 120_000
            return cachedFileToken
        })
        .finally(() => {
            pendingFileToken = null
        })

    return pendingFileToken
}

interface ImagePreviewProps {
    collectionId: string
    recordId: string
    files: string | string[]
    className?: string
    size?: 'sm' | 'md' | 'lg' | 'fill'
}

export function ImagePreview({
    collectionId,
    recordId,
    files,
    className,
    size = 'sm',
}: ImagePreviewProps) {
    const [selectedFile, setSelectedFile] = useState<string | null>(null)
    const [fileToken, setFileToken] = useState<string | null>(null)

    const fileList = Array.isArray(files) ? files : [files]
    // Filter out empty strings
    const validFiles = fileList.filter(f => !!f)

    useEffect(() => {
        let cancelled = false

        getFileToken()
            .then((token) => {
                if (!cancelled) {
                    setFileToken(token)
                }
            })
            .catch(() => {
                if (!cancelled) {
                    setFileToken('')
                }
            })

        return () => {
            cancelled = true
        }
    }, [])

    const getUrl = (filename: string) => {
        const path = [
            '/api/files',
            encodeURIComponent(collectionId),
            encodeURIComponent(recordId),
            encodeURIComponent(filename),
        ].join('/')
        return fileToken ? `${path}?token=${encodeURIComponent(fileToken)}` : path
    }

    const isImage = (filename: string) =>
        /\.(jpg|jpeg|png|gif|webp|svg)$/i.test(filename)

    const sizeClass = {
        sm: 'h-8 w-8',
        md: 'h-16 w-16',
        lg: 'w-full h-auto aspect-video',
        fill: 'h-full w-full',
    }[size]

    const handleNext = () => {
        if (!selectedFile) return
        const idx = validFiles.indexOf(selectedFile)
        const nextIdx = (idx + 1) % validFiles.length
        setSelectedFile(validFiles[nextIdx])
    }

    const handlePrev = () => {
        if (!selectedFile) return
        const idx = validFiles.indexOf(selectedFile)
        const prevIdx = (idx - 1 + validFiles.length) % validFiles.length
        setSelectedFile(validFiles[prevIdx])
    }

    // Handle keyboard navigation
    useEffect(() => {
        if (!selectedFile) return

        const handleKeyDown = (e: KeyboardEvent) => {
            if (e.key === 'ArrowRight') handleNext()
            if (e.key === 'ArrowLeft') handlePrev()
            if (e.key === 'Escape') setSelectedFile(null)
        }

        window.addEventListener('keydown', handleKeyDown)
        return () => window.removeEventListener('keydown', handleKeyDown)
    }, [selectedFile])

    if (validFiles.length === 0) return null

    return (
        <>
            <div className={cn('flex flex-wrap gap-2', className)}>
                {validFiles.map((file, idx) => (
                    <div
                        key={file}
                        className={cn(
                            'relative overflow-hidden rounded-md border bg-muted group cursor-pointer hover:ring-2 hover:ring-primary/50 transition-all isolate',
                            sizeClass
                        )}
                        onClick={(e) => {
                            e.stopPropagation()
                            setSelectedFile(file)
                        }}
                    >
                        {isImage(file) && fileToken === null ? (
                            <div className="flex h-full w-full items-center justify-center bg-secondary">
                                <ImageIcon className="h-4 w-4 text-muted-foreground" />
                            </div>
                        ) : isImage(file) ? (
                            <img
                                src={getUrl(file)}
                                alt={file}
                                className="h-full w-full object-cover"
                                loading="lazy"
                            />
                        ) : (
                            <div className="flex h-full w-full items-center justify-center bg-secondary">
                                <FileIcon className="h-4 w-4 text-muted-foreground" />
                            </div>
                        )}
                    </div>
                ))}
            </div>

            <Dialog open={!!selectedFile} onOpenChange={(open) => !open && setSelectedFile(null)}>
                <DialogContent
                    className="max-w-[95vw] max-h-[95vh] w-auto h-auto p-0 bg-transparent border-0 shadow-none sm:rounded-lg outline-none flex flex-col overflow-hidden"
                    onClick={(e) => e.stopPropagation()}
                    aria-describedby="image-preview-description"
                >
                    <div className="sr-only">
                        <DialogTitle>{selectedFile || 'Image Preview'}</DialogTitle>
                        <DialogDescription id="image-preview-description">
                            Preview of {selectedFile}
                        </DialogDescription>
                    </div>
                    {selectedFile && (
                        <div className="relative flex flex-col w-full h-full max-h-[90vh] bg-transparent">

                            {/* Image Area - Centered */}
                            <div className="flex-1 flex items-center justify-center min-h-[200px] overflow-hidden relative group">
                                {isImage(selectedFile) && fileToken === null ? (
                                    <div className="w-[600px] h-[400px] max-w-full flex items-center justify-center bg-white rounded-t-lg shadow-2xl">
                                        <ImageIcon className="h-16 w-16 text-slate-300" />
                                    </div>
                                ) : isImage(selectedFile) ? (
                                    <img
                                        src={getUrl(selectedFile)}
                                        alt={selectedFile}
                                        className="max-w-full max-h-[80vh] object-contain shadow-2xl rounded-t-lg bg-black/5"
                                    />
                                ) : (
                                    <div className="w-[600px] h-[400px] max-w-full flex flex-col items-center justify-center bg-white rounded-t-lg shadow-2xl gap-6">
                                        <FileIcon className="h-24 w-24 text-slate-300" />
                                        <p className="text-xl font-medium text-center break-all max-w-[80%]">{selectedFile}</p>
                                        <Button asChild size="lg">
                                            <a href={fileToken === null ? undefined : getUrl(selectedFile)} download target="_blank" rel="noreferrer">
                                                <Download className="mr-2 h-5 w-5" />
                                                Download
                                            </a>
                                        </Button>
                                    </div>
                                )}

                                {/* Navigation Arrows (Overlay on image area) */}
                                {validFiles.length > 1 && (
                                    <>
                                        <Button
                                            variant="ghost"
                                            size="icon"
                                            className="absolute left-4 top-1/2 -translate-y-1/2 rounded-full bg-black/20 hover:bg-black/40 text-white h-12 w-12 opacity-0 group-hover:opacity-100 transition-opacity"
                                            onClick={(e) => { e.stopPropagation(); handlePrev() }}
                                        >
                                            <ChevronLeft className="h-8 w-8" />
                                        </Button>
                                        <Button
                                            variant="ghost"
                                            size="icon"
                                            className="absolute right-4 top-1/2 -translate-y-1/2 rounded-full bg-black/20 hover:bg-black/40 text-white h-12 w-12 opacity-0 group-hover:opacity-100 transition-opacity"
                                            onClick={(e) => { e.stopPropagation(); handleNext() }}
                                        >
                                            <ChevronRight className="h-8 w-8" />
                                        </Button>
                                    </>
                                )}
                            </div>

                            {/* Footer - White background like PocketBase */}
                            <div className="bg-white p-4 flex items-center justify-between rounded-b-lg shadow-lg shrink-0">
                                <div className="flex items-center gap-2 overflow-hidden mr-4">
                                    <span className="text-sm font-medium truncate text-slate-700" title={selectedFile}>
                                        {selectedFile}
                                    </span>
                                    <a
                                        href={fileToken === null ? undefined : getUrl(selectedFile)}
                                        target="_blank"
                                        rel="noreferrer"
                                        className="text-slate-400 hover:text-blue-600 transition-colors shrink-0"
                                        title="Open in new tab"
                                    >
                                        <ExternalLink className="h-4 w-4" />
                                    </a>
                                </div>

                                <div className="flex items-center gap-4 shrink-0">
                                    {validFiles.length > 1 && (
                                        <span className="text-xs text-muted-foreground mr-2">
                                            {validFiles.indexOf(selectedFile) + 1} / {validFiles.length}
                                        </span>
                                    )}
                                    <Button
                                        variant="ghost"
                                        size="sm"
                                        onClick={() => setSelectedFile(null)}
                                        className="text-muted-foreground hover:text-foreground font-medium"
                                    >
                                        Close
                                    </Button>
                                </div>
                            </div>
                        </div>
                    )}
                </DialogContent>
            </Dialog>
        </>
    )
}
