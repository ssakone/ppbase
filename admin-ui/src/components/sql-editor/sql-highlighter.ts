/**
 * Applies SQL syntax highlighting to a SQL string.
 * Returns an HTML string with span elements for different token types
 * (styled by the `.sql-hl-*` classes in globals.css).
 *
 * The output keeps the exact characters of the input so it can be used as an
 * overlay behind a transparent textarea.
 */

const KEYWORDS = [
  'ABORT', 'ADD', 'ALL', 'ALTER', 'ANALYZE', 'AND', 'ANY', 'AS', 'ASC', 'BEGIN',
  'BETWEEN', 'BY', 'CALL', 'CASCADE', 'CASE', 'CAST', 'CHECK', 'COLLATE', 'COLUMN',
  'COMMENT', 'COMMIT', 'CONCURRENTLY', 'CONFLICT', 'CONSTRAINT', 'COPY', 'CREATE',
  'CROSS', 'CURRENT_DATE', 'CURRENT_TIME', 'CURRENT_TIMESTAMP', 'CURRENT_USER',
  'DEFAULT', 'DEFERRABLE', 'DELETE', 'DESC', 'DISTINCT', 'DO', 'DROP', 'ELSE', 'END',
  'ESCAPE', 'EXCEPT', 'EXECUTE', 'EXISTS', 'EXPLAIN', 'EXTENSION', 'FALSE', 'FETCH',
  'FILTER', 'FIRST', 'FOLLOWING', 'FOR', 'FOREIGN', 'FROM', 'FULL', 'FUNCTION',
  'GRANT', 'GROUP', 'HAVING', 'IF', 'ILIKE', 'IN', 'INDEX', 'INNER', 'INSERT',
  'INTERSECT', 'INTO', 'IS', 'ISNULL', 'JOIN', 'KEY', 'LANGUAGE', 'LAST', 'LATERAL',
  'LEFT', 'LIKE', 'LIMIT', 'LOCK', 'MATCHED', 'MATERIALIZED', 'MERGE', 'NATURAL',
  'NO', 'NOT', 'NOTHING', 'NOTNULL', 'NULL', 'NULLS', 'OFFSET', 'ON', 'ONLY', 'OR',
  'ORDER', 'OUTER', 'OVER', 'OWNER', 'PARTITION', 'PRECEDING', 'PRIMARY',
  'PROCEDURE', 'RANGE', 'RECURSIVE', 'REFERENCES', 'REFRESH', 'RENAME', 'REPLACE',
  'RESET', 'RESTRICT', 'RETURNING', 'RETURNS', 'REVOKE', 'RIGHT', 'ROLLBACK', 'ROW',
  'ROWS', 'SAVEPOINT', 'SCHEMA', 'SELECT', 'SEQUENCE', 'SET', 'SHOW', 'SIMILAR',
  'SOME', 'TABLE', 'TEMP', 'TEMPORARY', 'THEN', 'TO', 'TRANSACTION', 'TRIGGER',
  'TRUE', 'TRUNCATE', 'TYPE', 'UNBOUNDED', 'UNION', 'UNIQUE', 'UNLOGGED', 'UPDATE',
  'USING', 'VACUUM', 'VALUES', 'VIEW', 'WHEN', 'WHERE', 'WINDOW', 'WITH', 'WITHIN',
]

const TYPES = [
  'BIGINT', 'BIGSERIAL', 'BIT', 'BOOL', 'BOOLEAN', 'BYTEA', 'CHAR', 'CHARACTER',
  'CIDR', 'DATE', 'DECIMAL', 'DOUBLE', 'FLOAT', 'FLOAT4', 'FLOAT8', 'INET', 'INT',
  'INT2', 'INT4', 'INT8', 'INTEGER', 'INTERVAL', 'JSON', 'JSONB', 'MONEY', 'NUMERIC',
  'PRECISION', 'REAL', 'SERIAL', 'SMALLINT', 'TEXT', 'TIME', 'TIMESTAMP',
  'TIMESTAMPTZ', 'TIMETZ', 'TSVECTOR', 'UUID', 'VARCHAR', 'VARYING', 'XML', 'ZONE',
]

const words = (list: string[]) => `\\b(?:${list.join('|')})\\b`

// Alternation order matters: literals and comments first so keywords inside
// them are not highlighted, then keywords/types before generic functions.
const TOKEN_RE = new RegExp(
  [
    /(?<string>'(?:''|[^'])*(?:'|$))/.source,
    /(?<dollar>\$(?<tag>[A-Za-z_]\w*)?\$[\s\S]*?(?:\$\k<tag>\$|$))/.source,
    /(?<identifier>"(?:""|[^"])*(?:"|$))/.source,
    /(?<comment>--[^\n]*|\/\*[\s\S]*?(?:\*\/|$))/.source,
    `(?<keyword>${words(KEYWORDS)})`,
    `(?<type>${words(TYPES)})`,
    /(?<func>\b[A-Za-z_]\w*(?=\s*\())/.source,
    /(?<number>\b\d+(?:\.\d+)?(?:e[+-]?\d+)?\b)/.source,
    /(?<operator>[*=<>!|:+\-/%]+)/.source,
  ].join('|'),
  'gi',
)

const CLASS_BY_GROUP: Record<string, string> = {
  string: 'sql-hl-string',
  dollar: 'sql-hl-string',
  identifier: 'sql-hl-identifier',
  comment: 'sql-hl-comment',
  keyword: 'sql-hl-keyword',
  type: 'sql-hl-type',
  func: 'sql-hl-function',
  number: 'sql-hl-number',
  operator: 'sql-hl-operator',
}

function escapeHtml(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

export function highlightSQL(sql: string): string {
  if (!sql) return '\n'

  let result = ''
  let lastIdx = 0

  for (const m of sql.matchAll(TOKEN_RE)) {
    const index = m.index ?? 0
    result += escapeHtml(sql.substring(lastIdx, index))

    const group = Object.keys(CLASS_BY_GROUP).find((name) => m.groups?.[name] !== undefined)
    const token = escapeHtml(m[0])
    result += group ? `<span class="${CLASS_BY_GROUP[group]}">${token}</span>` : token

    lastIdx = index + m[0].length
  }

  result += escapeHtml(sql.substring(lastIdx))
  return result + '\n'
}
