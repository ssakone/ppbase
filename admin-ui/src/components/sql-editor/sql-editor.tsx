import { useState, useRef, useCallback, useEffect, KeyboardEvent } from 'react'
import { useDatabaseTables } from '@/hooks/use-db-tables'
import { highlightSQL } from './sql-highlighter'
import { SQL_KEYWORDS } from './sql-keywords'
import { SqlAutocomplete, AutocompleteItem } from './sql-autocomplete'

interface SqlEditorProps {
  value: string
  onChange: (value: string) => void
  placeholder?: string
}

export function SqlEditor({
  value,
  onChange,
  placeholder = 'SELECT id, created, updated FROM posts WHERE published = true',
}: SqlEditorProps) {
  const { data: tables = [] } = useDatabaseTables()
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const highlightRef = useRef<HTMLPreElement>(null)
  const wrapperRef = useRef<HTMLDivElement>(null)

  const [acItems, setAcItems] = useState<AutocompleteItem[]>([])
  const [acIndex, setAcIndex] = useState(0)
  const [acVisible, setAcVisible] = useState(false)
  const [acPosition, setAcPosition] = useState({ left: 0, top: 0 })

  const lineCount = (value || '').split('\n').length

  const syncScroll = useCallback(() => {
    if (textareaRef.current && highlightRef.current) {
      highlightRef.current.scrollTop = textareaRef.current.scrollTop
      highlightRef.current.scrollLeft = textareaRef.current.scrollLeft
    }
  }, [])

  const hideAutocomplete = useCallback(() => {
    setAcVisible(false)
    setAcItems([])
  }, [])

  const getCaretCoordinates = useCallback(() => {
    const textarea = textareaRef.current
    if (!textarea) return { left: 0, top: 0 }

    const mirror = document.createElement('div')
    const style = getComputedStyle(textarea)
    const props = [
      'fontFamily', 'fontSize', 'fontWeight', 'lineHeight', 'letterSpacing',
      'padding', 'paddingLeft', 'paddingTop', 'border', 'whiteSpace',
      'wordWrap', 'overflowWrap', 'tabSize',
    ] as const
    mirror.style.position = 'absolute'
    mirror.style.visibility = 'hidden'
    mirror.style.whiteSpace = 'pre-wrap'
    mirror.style.wordWrap = 'break-word'
    mirror.style.width = style.width
    for (const p of props) {
      ;(mirror.style as unknown as Record<string, string>)[p] = style.getPropertyValue(
        p.replace(/([A-Z])/g, '-$1').toLowerCase()
      )
    }

    const text = textarea.value.substring(0, textarea.selectionStart)
    mirror.textContent = text

    const span = document.createElement('span')
    span.textContent = textarea.value.substring(textarea.selectionStart) || '.'
    mirror.appendChild(span)

    document.body.appendChild(mirror)
    const left = span.offsetLeft - textarea.scrollLeft
    const top = span.offsetTop - textarea.scrollTop
    document.body.removeChild(mirror)

    return { left, top }
  }, [])

  const getCompletions = useCallback(
    (text: string, pos: number): AutocompleteItem[] => {
      const before = text.substring(0, pos)
      const match = before.match(/[\w.]*$/)
      if (!match || !match[0]) return []

      const word = match[0]
      const items: AutocompleteItem[] = []

      // Parse table aliases
      const aliases: Record<string, string> = {}
      const aliasRegex =
        /(?:FROM|JOIN)\s+(\w+)(?:\s+AS\s+(\w+)|\s+(\w+)(?=\s*(?:ON|WHERE|JOIN|LEFT|RIGHT|INNER|OUTER|FULL|CROSS|GROUP|ORDER|LIMIT|HAVING|UNION|,|\)|$)))/gi
      let m
      while ((m = aliasRegex.exec(text)) !== null) {
        const tableName = m[1].toLowerCase()
        const alias = (m[2] || m[3] || '').toLowerCase()
        if (alias && alias !== tableName) {
          const kwSet = new Set(SQL_KEYWORDS.map((k) => k.toLowerCase()))
          if (!kwSet.has(alias)) {
            aliases[alias] = tableName
          }
        }
      }

      // After "name." -> show columns
      if (word.includes('.')) {
        const parts = word.split('.')
        const prefix = parts[0].toLowerCase()
        const colPrefix = (parts[1] || '').toLowerCase()
        const realName = aliases[prefix] || prefix
        const table = tables.find((t) => t.name.toLowerCase() === realName)
        if (table) {
          for (const col of table.columns) {
            if (col.name.toLowerCase().startsWith(colPrefix)) {
              items.push({ label: col.name, kind: 'column', detail: col.type })
            }
          }
        }
        return items
      }

      const lower = word.toLowerCase()

      // Context detection
      const beforeWord = before.substring(0, before.length - word.length).trimEnd().toUpperCase()
      const lastKw = beforeWord.split(/\s+/).pop() || ''
      const tableContext = ['FROM', 'JOIN', 'INTO', 'TABLE', 'UPDATE'].includes(lastKw)

      // Table names
      for (const t of tables) {
        if (t.name.toLowerCase().startsWith(lower)) {
          items.push({ label: t.name, kind: 'table', detail: `${t.columns.length} cols` })
        }
      }

      // Column names (lower priority in table context)
      if (!tableContext) {
        for (const t of tables) {
          for (const col of t.columns) {
            if (col.name.toLowerCase().startsWith(lower)) {
              if (!items.find((i) => i.label === col.name && i.kind === 'column')) {
                items.push({ label: col.name, kind: 'column', detail: `${t.name}.${col.type}` })
              }
            }
          }
        }
      }

      // SQL keywords
      if (!tableContext) {
        for (const kw of SQL_KEYWORDS) {
          if (kw.toLowerCase().startsWith(lower) && lower.length >= 1) {
            items.push({ label: kw, kind: 'keyword' })
          }
        }
      }

      // Sort
      items.sort((a, b) => {
        const order: Record<string, number> = {
          table: tableContext ? 0 : 1,
          column: tableContext ? 2 : 0,
          keyword: 3,
        }
        return (order[a.kind] ?? 9) - (order[b.kind] ?? 9)
      })

      return items.slice(0, 12)
    },
    [tables],
  )

  const showAutocomplete = useCallback(
    (items: AutocompleteItem[]) => {
      if (items.length === 0) {
        hideAutocomplete()
        return
      }
      setAcItems(items)
      setAcIndex(0)
      setAcVisible(true)
      setAcPosition(getCaretCoordinates())
    },
    [hideAutocomplete, getCaretCoordinates],
  )

  const applyAutocomplete = useCallback(
    (index: number) => {
      const item = acItems[index]
      if (!item) return
      const textarea = textareaRef.current
      if (!textarea) return

      const pos = textarea.selectionStart
      const text = textarea.value
      const before = text.substring(0, pos)
      const match = before.match(/[\w.]*$/)
      const wordStart = match ? pos - match[0].length : pos

      let insertText = item.label
      if (match && match[0].includes('.')) {
        const dotPos = match[0].lastIndexOf('.')
        const prefix = match[0].substring(0, dotPos + 1)
        insertText = prefix + item.label
      }

      const newValue = text.substring(0, wordStart) + insertText + text.substring(pos)
      onChange(newValue)
      hideAutocomplete()

      requestAnimationFrame(() => {
        const newPos = wordStart + insertText.length
        textarea.selectionStart = textarea.selectionEnd = newPos
        textarea.focus()
      })
    },
    [acItems, onChange, hideAutocomplete],
  )

  const handleInput = useCallback(
    (newValue: string) => {
      onChange(newValue)
      const textarea = textareaRef.current
      if (!textarea) return
      requestAnimationFrame(() => {
        const pos = textarea.selectionStart
        const items = getCompletions(newValue, pos)
        showAutocomplete(items)
      })
    },
    [onChange, getCompletions, showAutocomplete],
  )

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      // Ctrl+Space force autocomplete
      if ((e.ctrlKey || e.metaKey) && e.key === ' ') {
        e.preventDefault()
        const textarea = textareaRef.current
        if (!textarea) return
        const items = getCompletions(textarea.value, textarea.selectionStart)
        showAutocomplete(items)
        return
      }

      // Tab inserts spaces (when AC not visible)
      if (e.key === 'Tab' && !acVisible) {
        e.preventDefault()
        const textarea = textareaRef.current
        if (!textarea) return
        const pos = textarea.selectionStart
        const newValue = value.substring(0, pos) + '  ' + value.substring(pos)
        onChange(newValue)
        requestAnimationFrame(() => {
          textarea.selectionStart = textarea.selectionEnd = pos + 2
        })
        return
      }

      if (acVisible) {
        if (e.key === 'ArrowDown') {
          e.preventDefault()
          setAcIndex((prev) => (prev + 1) % acItems.length)
          return
        }
        if (e.key === 'ArrowUp') {
          e.preventDefault()
          setAcIndex((prev) => (prev - 1 + acItems.length) % acItems.length)
          return
        }
        if (e.key === 'Enter' || e.key === 'Tab') {
          e.preventDefault()
          applyAutocomplete(acIndex)
          return
        }
        if (e.key === 'Escape') {
          e.preventDefault()
          hideAutocomplete()
          return
        }
      }
    },
    [acVisible, acItems.length, acIndex, value, onChange, getCompletions, showAutocomplete, applyAutocomplete, hideAutocomplete],
  )

  return (
    <div
      ref={wrapperRef}
      className="relative rounded-md border bg-background font-mono text-sm overflow-hidden"
      style={{ minHeight: 120 }}
    >
      {/* Line numbers */}
      <div
        className="absolute left-0 top-0 bottom-0 w-10 bg-muted/50 text-right pr-2 pt-[9px] text-xs text-muted-foreground select-none overflow-hidden leading-[1.5]"
        aria-hidden="true"
      >
        {Array.from({ length: lineCount }, (_, i) => (
          <div key={i}>{i + 1}</div>
        ))}
      </div>

      {/* Highlight overlay */}
      <pre
        ref={highlightRef}
        className="absolute inset-0 pl-12 pt-[9px] pr-3 pb-2 m-0 pointer-events-none overflow-auto whitespace-pre-wrap break-words leading-[1.5] text-sm"
        aria-hidden="true"
      >
        <code dangerouslySetInnerHTML={{ __html: highlightSQL(value) }} />
      </pre>

      {/* Textarea */}
      <textarea
        ref={textareaRef}
        className="relative w-full min-h-[120px] pl-12 pt-2 pr-3 pb-2 bg-transparent text-transparent caret-foreground resize-y outline-none leading-[1.5] text-sm"
        spellCheck={false}
        autoComplete="off"
        autoCapitalize="off"
        placeholder={placeholder}
        value={value}
        onChange={(e) => handleInput(e.target.value)}
        onScroll={syncScroll}
        onKeyDown={handleKeyDown}
        onBlur={() => setTimeout(hideAutocomplete, 150)}
      />

      {/* Autocomplete dropdown */}
      {acVisible && (
        <SqlAutocomplete
          items={acItems}
          activeIndex={acIndex}
          onSelect={applyAutocomplete}
          position={acPosition}
        />
      )}
    </div>
  )
}
