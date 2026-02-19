/**
 * Normalize search/filter input for the records API.
 *
 * Like PocketBase's admin UI:
 * - Plain text (e.g. "test") → builds filter searching across all searchable fields:
 *   scalar fields use ~ (ILIKE), array fields use ?~ (ANY ILIKE via unnest)
 * - Full filter expression → passed through as-is
 */

import type { Collection } from '@/api/types'

/** Scalar field types: use ~ (ILIKE) */
const SCALAR_SEARCHABLE = new Set(['text', 'editor', 'email', 'url'])

export type SearchableField = { name: string; op: '~' | '?~' }

/**
 * Returns fields that can be used for plain-text search.
 * - Scalar (text, id, single-select): use ~ (ILIKE)
 * - Array (select/file/relation multi): use ?~ (ANY ILIKE via unnest)
 * - JSON excluded: JSONB would need different handling
 */
export function getSearchableFields(collection: Collection): SearchableField[] {
  const fields = collection.fields ?? collection.schema ?? []
  const result: SearchableField[] = [{ name: 'id', op: '~' }]

  for (const f of fields) {
    if ((f as { hidden?: boolean }).hidden) continue
    if (f.type === 'password') continue
    if (f.type === 'json') continue // JSONB — ?~ uses unnest, not suitable
    const maxSelect = f.maxSelect ?? (f as { options?: { maxSelect?: number } }).options?.maxSelect ?? 1

    if (f.type === 'select') {
      result.push({ name: f.name, op: maxSelect > 1 ? '?~' : '~' })
    } else if (f.type === 'file' || f.type === 'relation') {
      result.push({ name: f.name, op: maxSelect > 1 ? '?~' : '~' })
    } else if (SCALAR_SEARCHABLE.has(f.type)) {
      result.push({ name: f.name, op: '~' })
    }
  }

  return result
}

/**
 * Heuristic: does the input look like a PocketBase filter expression?
 * (contains operators like ~, =, !=, &&, ||, >, <, ?~, etc.)
 */
function looksLikeFilterExpression(input: string): boolean {
  const trimmed = input.trim()
  if (!trimmed) return false

  // Contains filter operators (with optional field names before them)
  // Examples: name~'x', field = 'y', a && b, created > '2022-01-01'
  const hasOperator =
    /[a-zA-Z_][a-zA-Z0-9_]*\s*[~=!<>]=?|&&|\|\||\?~|\?=|\?!=|\?[<>]=?/.test(trimmed) ||
    trimmed.includes('~') ||
    trimmed.includes('&&') ||
    trimmed.includes('||') ||
    (trimmed.includes('=') && (trimmed.includes("'") || trimmed.includes('"'))) ||
    /[<>]=?/.test(trimmed)

  return !!hasOperator
}

/**
 * Escape a string for use inside single-quoted filter literal.
 * Doubles single quotes (SQL style).
 */
function escapeForFilter(value: string): string {
  return value.replace(/'/g, "''")
}

/**
 * Normalize search/filter input for the records API.
 *
 * - Empty → returns ""
 * - Plain text → (field1~'term' || field2?~'term' || ...)  (~ for scalar, ?~ for arrays)
 * - Filter expression → passed through as-is
 */
export function normalizeSearchFilter(
  filter: string,
  searchableFields: SearchableField[],
): string {
  const trimmed = (filter ?? '').trim()
  if (!trimmed) return ''

  if (looksLikeFilterExpression(trimmed)) {
    return trimmed
  }

  const escaped = escapeForFilter(trimmed)
  const parts = searchableFields.map((f) => `${f.name}${f.op}'${escaped}'`)
  return parts.length === 1 ? parts[0]! : `(${parts.join(' || ')})`
}
