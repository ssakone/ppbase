import { FIELD_TYPE_CONFIG } from '@/lib/field-types'

interface FieldTypeBadgeProps {
  type: string
  className?: string
}

export function FieldTypeBadge({ type, className }: FieldTypeBadgeProps) {
  const config = FIELD_TYPE_CONFIG[type]
  if (!config) return <span className={className}>{type}</span>

  return (
    <span
      className={className}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        width: '22px',
        height: '22px',
        borderRadius: '4px',
        fontSize: '11px',
        fontWeight: 600,
        backgroundColor: config.bg,
        color: config.color,
      }}
    >
      {config.icon}
    </span>
  )
}
