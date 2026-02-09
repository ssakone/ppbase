/**
 * Applies SQL syntax highlighting to a SQL string.
 * Returns an HTML string with span elements for different token types.
 */
export function highlightSQL(sql: string): string {
  if (!sql) return '\n'

  // Escape HTML first
  let escaped = sql
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')

  // Tokenize with regex
  // Order: strings first, then comments, then keywords, then numbers, then stars
  const regex = /('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")|(--.*)(\/\*[\s\S]*?\*\/)|(\b(?:SELECT|FROM|WHERE|AND|OR|NOT|IN|IS|NULL|AS|ON|JOIN|LEFT|RIGHT|INNER|OUTER|FULL|CROSS|ORDER|BY|GROUP|HAVING|LIMIT|OFFSET|UNION|ALL|INSERT|INTO|VALUES|UPDATE|SET|DELETE|CREATE|DROP|ALTER|TABLE|VIEW|INDEX|DISTINCT|COUNT|SUM|AVG|MIN|MAX|CASE|WHEN|THEN|ELSE|END|BETWEEN|LIKE|ILIKE|EXISTS|TRUE|FALSE|ASC|DESC|NULLS|FIRST|LAST|COALESCE|CAST|WITH|RECURSIVE|OVER|PARTITION|ROW_NUMBER|RANK|DENSE_RANK|LATERAL|FILTER|ARRAY_AGG|STRING_AGG|JSON_AGG|JSONB_AGG|NOW|CURRENT_TIMESTAMP|EXTRACT|INTERVAL|DATE|TIMESTAMP|TIMESTAMPTZ|INTEGER|TEXT|BOOLEAN|DOUBLE|PRECISION|VARCHAR|JSONB)\b)|(\b\d+(?:\.\d+)?\b)|(\*)/gi

  let result = ''
  let lastIdx = 0

  for (const m of escaped.matchAll(regex)) {
    // Append non-matched text
    result += escaped.substring(lastIdx, m.index)

    if (m[1]) {
      result += `<span class="sql-hl-string">${m[0]}</span>`
    } else if (m[2]) {
      result += `<span class="sql-hl-comment">${m[0]}</span>`
    } else if (m[3]) {
      result += `<span class="sql-hl-comment">${m[0]}</span>`
    } else if (m[4]) {
      result += `<span class="sql-hl-keyword">${m[0].toUpperCase()}</span>`
    } else if (m[5]) {
      result += `<span class="sql-hl-number">${m[0]}</span>`
    } else if (m[6]) {
      result += `<span class="sql-hl-star">${m[0]}</span>`
    }

    lastIdx = (m.index ?? 0) + m[0].length
  }

  result += escaped.substring(lastIdx)
  return result + '\n'
}
