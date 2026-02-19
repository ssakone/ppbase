import { Loader2 } from 'lucide-react'
import { cn } from '@/lib/utils'

interface LoadingSpinnerProps {
  className?: string
  size?: 'sm' | 'md' | 'lg'
  fullPage?: boolean
}

export function LoadingSpinner({ className, size = 'md', fullPage = false }: LoadingSpinnerProps) {
  const sizeClasses = {
    sm: 'h-4 w-4',
    md: 'h-8 w-8',
    lg: 'h-12 w-12',
  }

  if (fullPage) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <Loader2
          className={cn(
            'animate-spin text-indigo-400',
            sizeClasses[size],
            className,
          )}
        />
      </div>
    )
  }

  return (
    <Loader2
      className={cn('animate-spin', sizeClasses[size], className)}
    />
  )
}
