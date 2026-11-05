import { useCallback, useEffect, useState, type KeyboardEvent } from 'react'
import { ContentHeader } from '@/components/content-header'
import { Breadcrumb } from '@/components/breadcrumb'
import { SqlEditor } from '@/components/sql-editor/sql-editor'
import { highlightSQL } from '@/components/sql-editor/sql-highlighter'
import { LoadingSpinner } from '@/components/loading-spinner'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { useExecuteSql } from '@/hooks/use-sql'
import { cn } from '@/lib/utils'
import { AlertCircle, AlertTriangle, CheckCircle2, Play } from 'lucide-react'
import { toast } from 'sonner'
import type { ApiResponseError } from '@/api/response'
import type {
  SqlErrorData,
  SqlExecuteResult,
  SqlStatementKind,
  SqlStatementResult,
} from '@/api/types'

const QUERY_STORAGE_KEY = 'ppbase_sql_console_query'
const MAX_ROWS_OPTIONS = ['100', '500', '1000', '5000']

const KIND_META: Record<SqlStatementKind, { label: string; className: string }> = {
  read: { label: 'Read', className: 'bg-emerald-50 text-emerald-700 border-emerald-200' },
  modify: { label: 'Modification', className: 'bg-amber-50 text-amber-700 border-amber-200' },
  delete: { label: 'Deletion', className: 'bg-red-50 text-red-700 border-red-200' },
  schema: { label: 'Schema alteration', className: 'bg-violet-50 text-violet-700 border-violet-200' },
  maintenance: { label: 'Maintenance', className: 'bg-sky-50 text-sky-700 border-sky-200' },
  other: { label: 'Other', className: 'bg-slate-50 text-slate-600 border-slate-200' },
}

interface PendingConfirmation {
  query: string
  statements: SqlStatementResult[]
}

type SqlError = ApiResponseError & { data?: SqlErrorData }

function KindBadge({ kind }: { kind: SqlStatementKind }) {
  const meta = KIND_META[kind] ?? KIND_META.other
  return (
    <Badge variant="outline" className={cn('text-[11px] font-medium', meta.className)}>
      {meta.label}
    </Badge>
  )
}

function SqlSnippet({ sql, className }: { sql: string; className?: string }) {
  return (
    <pre
      className={cn(
        'rounded-md border bg-slate-50 px-3 py-2 font-mono text-xs whitespace-pre-wrap break-words overflow-auto',
        className,
      )}
    >
      <code dangerouslySetInnerHTML={{ __html: highlightSQL(sql) }} />
    </pre>
  )
}

function formatCell(value: unknown): { text: string; isNull: boolean } {
  if (value === null || value === undefined) return { text: 'NULL', isNull: true }
  if (typeof value === 'object') return { text: JSON.stringify(value), isNull: false }
  return { text: String(value), isNull: false }
}

function StatementResultCard({ result }: { result: SqlStatementResult }) {
  const columns = result.columns ?? []
  const rows = result.rows ?? []

  return (
    <div className="rounded-lg border bg-white shadow-sm overflow-hidden">
      <div className="flex flex-wrap items-center gap-2 px-4 py-2.5 border-b bg-slate-50/60">
        <span className="text-xs text-slate-400 font-mono">#{result.index + 1}</span>
        <KindBadge kind={result.kind} />
        <span className="text-sm font-medium text-slate-800">{result.status ?? result.command}</span>
        {result.truncated && (
          <span className="text-xs text-amber-600">
            Only the first {result.rowCount} rows are shown
          </span>
        )}
        <span className="ml-auto text-xs text-slate-400">
          {result.durationMs !== undefined ? `${result.durationMs} ms` : ''}
        </span>
      </div>
      <div className="px-4 py-2 border-b">
        <code
          className="block text-xs text-slate-500 font-mono truncate"
          title={result.sql}
          dangerouslySetInnerHTML={{ __html: highlightSQL(result.sql) }}
        />
      </div>
      {columns.length > 0 ? (
        rows.length > 0 ? (
          <div className="max-h-[420px] overflow-auto">
            <Table className="text-[13px]">
              <TableHeader className="sticky top-0 z-10">
                <TableRow>
                  {columns.map((col, i) => (
                    <TableHead key={`${col.name}-${i}`} className="whitespace-nowrap">
                      <span className="font-semibold text-slate-700">{col.name}</span>
                      <span className="ml-1.5 text-[11px] font-normal text-slate-400">{col.type}</span>
                    </TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((row, rowIndex) => (
                  <TableRow key={rowIndex}>
                    {row.map((value, colIndex) => {
                      const cell = formatCell(value)
                      return (
                        <TableCell
                          key={colIndex}
                          className={cn(
                            'max-w-[360px] truncate font-mono text-xs py-1.5',
                            cell.isNull && 'italic text-slate-400',
                          )}
                          title={cell.text}
                        >
                          {cell.text}
                        </TableCell>
                      )
                    })}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        ) : (
          <p className="px-4 py-3 text-sm text-slate-500">No rows returned.</p>
        )
      ) : (
        <p className="px-4 py-3 text-sm text-slate-500 flex items-center gap-1.5">
          <CheckCircle2 className="h-4 w-4 text-emerald-600" />
          Statement executed
          {typeof result.rowCount === 'number' && ` — ${result.rowCount} row(s) affected`}.
        </p>
      )}
    </div>
  )
}

function ConfirmSqlDialog({
  pending,
  isRunning,
  onCancel,
  onConfirm,
}: {
  pending: PendingConfirmation | null
  isRunning: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  const statements = pending?.statements ?? []
  const mutating = statements.filter((s) => s.mutating)
  const readCount = statements.length - mutating.length

  return (
    <Dialog open={pending !== null} onOpenChange={(open) => !open && !isRunning && onCancel()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <AlertTriangle className="h-5 w-5 text-amber-500" />
            Confirm SQL execution
          </DialogTitle>
          <DialogDescription>
            This script will change the database. Review the{' '}
            {mutating.length === 1 ? 'statement' : `${mutating.length} statements`} below before
            running it. The whole script runs in a single transaction and is rolled back if any
            statement fails.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3 max-h-[50vh] overflow-auto pr-1">
          {mutating.map((stmt) => (
            <div key={stmt.index} className="space-y-1.5">
              <div className="flex items-center gap-2">
                <span className="text-xs text-slate-400 font-mono">#{stmt.index + 1}</span>
                <KindBadge kind={stmt.kind} />
                <span className="text-sm font-medium text-slate-800">{stmt.command}</span>
              </div>
              <SqlSnippet sql={stmt.sql} className="max-h-40" />
              {stmt.warning && (
                <p className="flex items-start gap-1.5 text-xs text-red-600">
                  <AlertTriangle className="h-3.5 w-3.5 mt-px shrink-0" />
                  {stmt.warning}
                </p>
              )}
            </div>
          ))}
          {readCount > 0 && (
            <p className="text-xs text-slate-500">
              The script also contains {readCount} read-only statement{readCount > 1 ? 's' : ''}.
            </p>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onCancel} disabled={isRunning}>
            Cancel
          </Button>
          <Button variant="destructive" onClick={onConfirm} disabled={isRunning}>
            {isRunning && <LoadingSpinner size="sm" className="mr-2" />}
            Execute
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export function SqlConsolePage() {
  const [query, setQuery] = useState(() => localStorage.getItem(QUERY_STORAGE_KEY) ?? '')
  const [maxRows, setMaxRows] = useState('500')
  const [result, setResult] = useState<SqlExecuteResult | null>(null)
  const [error, setError] = useState<SqlError | null>(null)
  const [pending, setPending] = useState<PendingConfirmation | null>(null)
  const executeSql = useExecuteSql()

  useEffect(() => {
    localStorage.setItem(QUERY_STORAGE_KEY, query)
  }, [query])

  const run = useCallback(
    async (sql: string, confirm: boolean) => {
      if (!sql.trim() || executeSql.isPending) return
      setError(null)
      try {
        const res = await executeSql.mutateAsync({ query: sql, confirm, maxRows: Number(maxRows) })
        if (res.requiresConfirmation) {
          setPending({ query: sql, statements: res.statements })
          return
        }
        setPending(null)
        setResult(res)
        if (res.committed) {
          toast.success('SQL script executed and committed')
        }
      } catch (err) {
        setPending(null)
        setResult(null)
        setError(err as SqlError)
      }
    },
    [executeSql, maxRows],
  )

  const handleKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    // Ctrl/Cmd+Enter runs the script (unless the editor used it for autocomplete).
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && !e.defaultPrevented) {
      e.preventDefault()
      void run(query, false)
    }
  }

  const errorData = error?.data

  return (
    <>
      <ContentHeader
        left={<Breadcrumb items={[{ label: 'SQL Console', active: true }]} />}
        right={
          <>
            <Select value={maxRows} onValueChange={setMaxRows}>
              <SelectTrigger className="w-[160px] h-9">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {MAX_ROWS_OPTIONS.map((value) => (
                  <SelectItem key={value} value={value}>
                    {value} rows max
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button
              size="sm"
              onClick={() => void run(query, false)}
              disabled={!query.trim() || executeSql.isPending}
            >
              {executeSql.isPending ? (
                <LoadingSpinner size="sm" className="mr-1.5" />
              ) : (
                <Play className="h-4 w-4 mr-1.5" />
              )}
              Run
            </Button>
          </>
        }
      />

      <div className="flex-1 overflow-auto p-4 md:p-6 space-y-4">
        <div onKeyDown={handleKeyDown} className="space-y-1.5">
          <SqlEditor
            value={query}
            onChange={setQuery}
            minHeight={220}
            placeholder="SELECT * FROM _collections LIMIT 10;"
          />
          <p className="text-xs text-muted-foreground">
            Press <kbd className="bg-muted px-1 rounded text-[11px] border">Ctrl/Cmd+Enter</kbd> to
            run, <kbd className="bg-muted px-1 rounded text-[11px] border">Ctrl+Space</kbd> for
            autocomplete. Separate multiple statements with <code>;</code>.
          </p>
        </div>

        {error && (
          <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 space-y-2">
            <p className="flex items-start gap-2 text-sm font-medium text-red-700">
              <AlertCircle className="h-4 w-4 mt-0.5 shrink-0" />
              <span>
                {typeof errorData?.statementIndex === 'number' &&
                  `Statement #${errorData.statementIndex + 1}: `}
                {error.message}
              </span>
            </p>
            {errorData?.detail && <p className="text-xs text-red-700">Detail: {errorData.detail}</p>}
            {errorData?.hint && <p className="text-xs text-red-700">Hint: {errorData.hint}</p>}
            {errorData?.sql && <SqlSnippet sql={errorData.sql} className="bg-white max-h-40" />}
            {errorData?.sqlstate && (
              <p className="text-[11px] text-red-500 font-mono">SQLSTATE {errorData.sqlstate}</p>
            )}
          </div>
        )}

        {result && (
          <div className="space-y-3">
            <p className="text-xs text-slate-500">
              {result.statements.length} statement{result.statements.length > 1 ? 's' : ''}{' '}
              executed in {result.durationMs} ms ·{' '}
              {result.committed ? 'committed' : 'read-only transaction (nothing was changed)'}
            </p>
            {result.statements.map((stmt) => (
              <StatementResultCard key={stmt.index} result={stmt} />
            ))}
          </div>
        )}
      </div>

      <ConfirmSqlDialog
        pending={pending}
        isRunning={executeSql.isPending}
        onCancel={() => setPending(null)}
        onConfirm={() => pending && void run(pending.query, true)}
      />
    </>
  )
}
