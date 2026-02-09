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
import { cn } from '@/lib/utils'

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

    const fileList = Array.isArray(files) ? files : [files]
    // Filter out empty strings
    const validFiles = fileList.filter(f => !!f)

    const getUrl = (filename: string) =>
        `/api/files/${collectionId}/${recordId}/${filename}`

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
                        {isImage(file) ? (
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
                                {isImage(selectedFile) ? (
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
                                            <a href={getUrl(selectedFile)} download target="_blank" rel="noreferrer">
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
                                        href={getUrl(selectedFile)}
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
