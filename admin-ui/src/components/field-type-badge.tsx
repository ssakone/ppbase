import { FIELD_TYPE_CONFIG } from '@/lib/field-types'
import { cn } from '@/lib/utils'

interface FieldTypeBadgeProps {
  type: string
  className?: string
  /** Use smaller inline style for table headers */
  inline?: boolean
}

export function FieldTypeBadge({ type, className, inline }: FieldTypeBadgeProps) {
  const config = FIELD_TYPE_CONFIG[type]
  if (!config) return <span className={className}>{type}</span>

  const size = inline ? '18px' : '22px'
  const font = inline ? '9px' : '11px'

  return (
    <span
      className={cn('shrink-0', className)}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        width: size,
        height: size,
        borderRadius: '4px',
        fontSize: font,
        fontWeight: 600,
        backgroundColor: config.bg,
        color: config.color,
        lineHeight: 1,
      }}
      title={config.label}
    >
      {config.icon}
    </span>
  )
}
