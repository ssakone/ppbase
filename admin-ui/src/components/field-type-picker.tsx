import { FIELD_TYPES, FIELD_TYPE_CONFIG } from '@/lib/field-types'

interface FieldTypePickerProps {
  onSelect: (type: string) => void
  onClose: () => void
}

export function FieldTypePicker({ onSelect, onClose }: FieldTypePickerProps) {
  return (
    <div className="rounded-lg border bg-background p-3 mb-3 shadow-sm">
      <div className="grid grid-cols-3 gap-2">
        {FIELD_TYPES.map((type) => {
          const config = FIELD_TYPE_CONFIG[type]
          if (!config) return null
          return (
            <button
              key={type}
              type="button"
              className="flex items-center gap-2 rounded-md px-3 py-2 text-sm hover:bg-muted transition-colors text-left"
              onClick={() => {
                onSelect(type)
                onClose()
              }}
            >
              <span
                className="inline-flex items-center justify-center rounded text-xs font-semibold shrink-0"
                style={{
                  width: 22,
                  height: 22,
                  backgroundColor: config.bg,
                  color: config.color,
                  fontSize: 11,
                }}
              >
                {config.icon}
              </span>
              <span className="whitespace-nowrap">{config.label}</span>
            </button>
          )
        })}
      </div>
    </div>
  )
}
