/**
 * Shared single/multiple rule for select, file and relation fields.
 *
 * Mirrors ppbase/models/field_multiplicity.py, itself based on PocketBase:
 * - v0.22.34 relation `MaxSelect *int`: `null` means an unlimited multiple
 *   relation (`IsMultiple() = MaxSelect == nil || *MaxSelect > 1`);
 * - select/file and the current PocketBase format: multiple iff `maxSelect > 1`;
 * - an absent key keeps a relation single (PocketBase 0.22 always sends the
 *   key; the current format decodes an absent key as 0).
 *
 * Nested `options` win over flat keys, like the server-side merge.
 */

/** Accepts API fields (flat or nested); `Field` from api/types fits. */
export type FieldLike = { type: string; options?: unknown; maxSelect?: unknown }

const ABSENT: unique symbol = Symbol('absent')

function rawMaxSelect(field: FieldLike): unknown {
  const options = field.options
  if (options && typeof options === 'object' && 'maxSelect' in options) {
    return (options as { maxSelect?: unknown }).maxSelect
  }
  if ('maxSelect' in field) return field.maxSelect
  return ABSENT
}

function numericMaxSelect(raw: unknown): number | null {
  if (typeof raw === 'number' && Number.isFinite(raw)) return Math.trunc(raw)
  if (typeof raw === 'boolean') return raw ? 1 : 0
  if (typeof raw === 'string' && raw.trim() !== '') {
    const parsed = Number(raw.trim())
    return Number.isInteger(parsed) ? parsed : null
  }
  return null
}

/** True for a relation saved by PocketBase 0.22 with `maxSelect: null`. */
export function hasUnlimitedRelationMaxSelect(field: FieldLike): boolean {
  return field.type === 'relation' && rawMaxSelect(field) === null
}

export function isRelationMultiple(field: FieldLike): boolean {
  const raw = rawMaxSelect(field)
  if (raw === null) return true
  if (raw === ABSENT) return false
  const value = numericMaxSelect(raw)
  return value !== null && value > 1
}

export function isMultiValueField(field: FieldLike): boolean {
  if (field.type === 'relation') return isRelationMultiple(field)
  if (field.type !== 'select' && field.type !== 'file') return false
  const raw = rawMaxSelect(field)
  if (raw === null || raw === ABSENT) return false
  const value = numericMaxSelect(raw)
  return value !== null && value > 1
}

/** Value used by the relation "Max select" picker; 'null' keeps the 0.22 unlimited form. */
export function relationMaxSelectChoice(field: FieldLike): string {
  if (hasUnlimitedRelationMaxSelect(field)) return 'null'
  const value = numericMaxSelect(rawMaxSelect(field))
  return String(value !== null && value > 0 ? value : 1)
}

/** Inverse of relationMaxSelectChoice. */
export function parseRelationMaxSelectChoice(choice: string): number | null {
  if (choice === 'null') return null
  const value = Number.parseInt(choice, 10)
  return Number.isFinite(value) ? value : 1
}
