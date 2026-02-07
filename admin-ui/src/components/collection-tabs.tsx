import { useState } from 'react'
import { Field, Collection } from '@/api/types'
import { useCollections } from '@/hooks/use-collections'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Button } from '@/components/ui/button'
import { SystemFieldsDisplay } from '@/components/system-fields-display'
import { FieldList } from '@/components/field-list'
import { FieldTypePicker } from '@/components/field-type-picker'
import { SqlEditor } from '@/components/sql-editor/sql-editor'
import { Plus } from 'lucide-react'

interface CollectionTabsProps {
  type: 'base' | 'auth' | 'view'
  fields: Field[]
  setFields: (fields: Field[]) => void
  rules: {
    listRule: string
    viewRule: string
    createRule: string
    updateRule: string
    deleteRule: string
  }
  setRules: (rules: CollectionTabsProps['rules']) => void
  viewQuery: string
  setViewQuery: (query: string) => void
}

export function CollectionTabs({
  type,
  fields,
  setFields,
  rules,
  setRules,
  viewQuery,
  setViewQuery,
}: CollectionTabsProps) {
  const [showPicker, setShowPicker] = useState(false)
  const { data: collections = [] } = useCollections()

  const defaultTab = type === 'view' ? 'query' : 'fields'

  const addField = (fieldType: string) => {
    setFields([...fields, { name: '', type: fieldType, required: false }])
    setShowPicker(false)
  }

  return (
    <Tabs defaultValue={defaultTab}>
      <TabsList>
        {type !== 'view' && <TabsTrigger value="fields">Fields</TabsTrigger>}
        <TabsTrigger value="api-rules">API Rules</TabsTrigger>
        {type === 'auth' && <TabsTrigger value="options">Options</TabsTrigger>}
        {type === 'view' && <TabsTrigger value="query">Query</TabsTrigger>}
      </TabsList>

      {type !== 'view' && (
        <TabsContent value="fields" className="space-y-3">
          <SystemFieldsDisplay type={type} />

          <FieldList
            fields={fields}
            onChange={setFields}
            collections={collections}
          />

          {showPicker && (
            <FieldTypePicker
              onSelect={addField}
              onClose={() => setShowPicker(false)}
            />
          )}

          <Button
            type="button"
            variant="outline"
            className="w-full border-dashed"
            onClick={() => setShowPicker(!showPicker)}
          >
            <Plus className="h-3 w-3 mr-2" />
            New field
          </Button>
        </TabsContent>
      )}

      <TabsContent value="api-rules" className="space-y-4">
        <div className="space-y-1.5">
          <Label className="text-sm">List/Search rule</Label>
          <Input
            placeholder="Leave empty for public access, or enter a filter expression"
            value={rules.listRule}
            onChange={(e) => setRules({ ...rules, listRule: e.target.value })}
          />
        </div>
        <div className="space-y-1.5">
          <Label className="text-sm">View rule</Label>
          <Input
            placeholder="Leave empty for public access"
            value={rules.viewRule}
            onChange={(e) => setRules({ ...rules, viewRule: e.target.value })}
          />
        </div>
        <div className="space-y-1.5">
          <Label className="text-sm">Create rule</Label>
          <Input
            placeholder="Leave empty for public access"
            value={rules.createRule}
            onChange={(e) => setRules({ ...rules, createRule: e.target.value })}
          />
        </div>
        <div className="space-y-1.5">
          <Label className="text-sm">Update rule</Label>
          <Input
            placeholder="Leave empty for public access"
            value={rules.updateRule}
            onChange={(e) => setRules({ ...rules, updateRule: e.target.value })}
          />
        </div>
        <div className="space-y-1.5">
          <Label className="text-sm">Delete rule</Label>
          <Input
            placeholder="Leave empty for public access"
            value={rules.deleteRule}
            onChange={(e) => setRules({ ...rules, deleteRule: e.target.value })}
          />
        </div>
        <p className="text-xs text-muted-foreground">
          Set to <code className="bg-muted px-1 rounded text-[11px]">null</code> (leave blank) for admin-only, or an empty string for public. Use filter expressions to control access.
        </p>
      </TabsContent>

      {type === 'auth' && (
        <TabsContent value="options">
          <p className="text-sm text-muted-foreground">Auth collection options will appear here.</p>
        </TabsContent>
      )}

      {type === 'view' && (
        <TabsContent value="query" className="space-y-3">
          <div className="space-y-1.5">
            <Label className="text-sm">
              Select query <span className="text-muted-foreground">*</span>
            </Label>
            <SqlEditor
              value={viewQuery}
              onChange={setViewQuery}
            />
            <p className="text-xs text-muted-foreground">
              SQL SELECT query that defines this view. Must include id, created, and updated columns.
              Press <kbd className="bg-muted px-1 rounded text-[11px] border">Ctrl+Space</kbd> for autocomplete.
            </p>
          </div>
          <ul className="text-sm text-muted-foreground space-y-1.5 pl-5 list-disc">
            <li>
              Wildcard columns (<code className="bg-muted px-1 rounded text-[11px]">*</code>) are not supported.
            </li>
            <li>
              The query must have a unique <code className="bg-muted px-1 rounded text-[11px]">id</code> column.
              <br />
              If your query doesn't have a suitable one, you can use{' '}
              <code className="bg-muted px-1 rounded text-[11px]">(ROW_NUMBER() OVER()) as id</code>.
            </li>
            <li>
              Expressions must be aliased with a valid formatted field name, e.g.{' '}
              <code className="bg-muted px-1 rounded text-[11px]">MAX(balance) as maxBalance</code>.
            </li>
            <li>
              Combined/multi-spaced expressions must be wrapped in parenthesis, e.g.
              <br />
              <code className="bg-muted px-1 rounded text-[11px]">(MAX(balance) + 1) as maxBalance</code>.
            </li>
          </ul>
        </TabsContent>
      )}
    </Tabs>
  )
}
