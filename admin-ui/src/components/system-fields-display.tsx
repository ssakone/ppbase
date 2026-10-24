import { FIELD_TYPE_CONFIG } from '@/lib/field-types'
import { Badge } from '@/components/ui/badge'
import type { Field } from '@/api/types'

interface SystemFieldsDisplayProps {
  type: 'base' | 'auth' | 'view'
  fields: Field[]
}

function SystemFieldRow({
  icon,
  bg,
  color,
  name,
  badges,
}: {
  icon: string
  bg: string
  color: string
  name: string
  badges?: { label: string; variant: 'green' | 'red' }[]
}) {
  return (
    <div className="flex items-center gap-2.5 px-3 py-2 text-muted-foreground">
      <span
        className="inline-flex items-center justify-center rounded text-xs font-semibold shrink-0"
        style={{
          width: 22,
          height: 22,
          backgroundColor: bg,
          color: color,
          fontSize: 11,
        }}
      >
        {icon}
      </span>
      <span className="text-sm text-muted-foreground">{name}</span>
      {badges && badges.length > 0 && (
        <span className="flex items-center gap-1 ml-auto">
          {badges.map((b, i) => (
            <Badge
              key={i}
              variant="outline"
              className={
                b.variant === 'green'
                  ? 'bg-green-100 text-green-700 border-green-200 text-[10px] px-1.5 py-0'
                  : 'bg-red-100 text-red-700 border-red-200 text-[10px] px-1.5 py-0'
              }
            >
              {b.label}
            </Badge>
          ))}
        </span>
      )}
    </div>
  )
}

function SystemDateRow({ name, info }: { name: string; info: string }) {
  const cfg = FIELD_TYPE_CONFIG.date
  return (
    <div className="flex items-center gap-2.5 px-3 py-2">
      <span
        className="inline-flex items-center justify-center rounded text-xs font-semibold shrink-0"
        style={{
          width: 22,
          height: 22,
          backgroundColor: cfg.bg,
          color: cfg.color,
          fontSize: 11,
        }}
      >
        {cfg.icon}
      </span>
      <span className="text-sm text-muted-foreground">{name}</span>
      <span className="ml-auto">
        <span className="text-[11px] text-muted-foreground bg-muted rounded px-1.5 py-0.5">
          {info}
        </span>
      </span>
    </div>
  )
}

export function SystemFieldsDisplay({ type, fields }: SystemFieldsDisplayProps) {
  const cfg = FIELD_TYPE_CONFIG
  const fieldNames = new Set(fields.map((field) => field.name))
  return (
    <div className="rounded-lg border bg-muted/30 mb-3">
      {!fieldNames.has('id') && (
        <SystemFieldRow
          icon="T"
          bg={cfg.text.bg}
          color={cfg.text.color}
          name="id"
          badges={[{ label: 'Nonempty', variant: 'green' }]}
        />
      )}

      {type === 'auth' && (
        <>
          {!fieldNames.has('password') && <SystemFieldRow
            icon="P"
            bg="#f8fafc"
            color="#475569"
            name="password"
            badges={[
              { label: 'Nonempty', variant: 'green' },
              { label: 'Hidden', variant: 'red' },
            ]}
          />}
          {!fieldNames.has('tokenKey') && <SystemFieldRow
            icon="T"
            bg={cfg.text.bg}
            color={cfg.text.color}
            name="tokenKey"
            badges={[
              { label: 'Nonempty', variant: 'green' },
              { label: 'Hidden', variant: 'red' },
            ]}
          />}
          {!fieldNames.has('email') && <SystemFieldRow
            icon="@"
            bg={cfg.email.bg}
            color={cfg.email.color}
            name="email"
            badges={[{ label: 'Nonempty', variant: 'green' }]}
          />}
          {!fieldNames.has('emailVisibility') && <SystemFieldRow
            icon="B"
            bg={cfg.bool.bg}
            color={cfg.bool.color}
            name="emailVisibility"
          />}
          {!fieldNames.has('verified') && <SystemFieldRow
            icon="B"
            bg={cfg.bool.bg}
            color={cfg.bool.color}
            name="verified"
          />}
        </>
      )}

      <SystemDateRow name="created" info="Create" />
      <SystemDateRow name="updated" info="Create/Update" />
    </div>
  )
}
