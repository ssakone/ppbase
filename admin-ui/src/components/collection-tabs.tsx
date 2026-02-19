import { useState } from 'react'
import { Field, OAuth2ProviderConfig, CollectionOAuth2Options } from '@/api/types'
import { useCollections } from '@/hooks/use-collections'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { SystemFieldsDisplay } from '@/components/system-fields-display'
import { FieldList } from '@/components/field-list'
import { FieldTypePicker } from '@/components/field-type-picker'
import { SqlEditor } from '@/components/sql-editor/sql-editor'
import { Plus, ChevronDown, ChevronUp } from 'lucide-react'

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
  oauth2Options?: CollectionOAuth2Options
  setOAuth2Options?: (opts: CollectionOAuth2Options) => void
}

const PROVIDERS = ['google', 'github', 'gitlab', 'discord', 'facebook'] as const

const DEFAULT_OAUTH2: CollectionOAuth2Options = {
  enabled: false,
  mappedFields: { name: 'name', username: 'username', avatarURL: 'avatar' },
  providers: [],
}

function ProviderCard({
  name,
  config,
  onChange,
}: {
  name: string
  config?: OAuth2ProviderConfig
  onChange: (cfg: OAuth2ProviderConfig | null) => void
}) {
  const [open, setOpen] = useState(false)
  const enabled = !!config

  const toggle = () => {
    if (enabled) {
      onChange(null)
      setOpen(false)
    } else {
      onChange({ name, clientId: '', clientSecret: '' })
      setOpen(true)
    }
  }

  return (
    <div className="rounded-md border bg-white">
      <div
        className="flex items-center justify-between px-4 py-3 cursor-pointer select-none"
        onClick={() => enabled && setOpen((o) => !o)}
      >
        <div className="flex items-center gap-3">
          <Checkbox checked={enabled} onCheckedChange={toggle} onClick={(e) => e.stopPropagation()} />
          <span className="font-medium capitalize">{name}</span>
        </div>
        {enabled && (
          <button
            type="button"
            className="text-muted-foreground"
            onClick={(e) => { e.stopPropagation(); setOpen((o) => !o) }}
          >
            {open ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
          </button>
        )}
      </div>
      {enabled && open && (
        <div className="border-t px-4 py-3 space-y-3">
          <div className="space-y-1.5">
            <Label className="text-xs">Client ID</Label>
            <Input
              placeholder="Client ID"
              value={config?.clientId ?? ''}
              onChange={(e) => onChange({ ...config!, clientId: e.target.value })}
            />
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs">Client Secret</Label>
            <Input
              type="password"
              placeholder="Client Secret"
              value={config?.clientSecret ?? ''}
              onChange={(e) => onChange({ ...config!, clientSecret: e.target.value })}
            />
          </div>
        </div>
      )}
    </div>
  )
}

function OAuth2Tab({
  opts,
  setOpts,
}: {
  opts: CollectionOAuth2Options
  setOpts: (o: CollectionOAuth2Options) => void
}) {
  const providerMap = Object.fromEntries(opts.providers.map((p) => [p.name, p]))

  const updateProvider = (name: string, cfg: OAuth2ProviderConfig | null) => {
    const next = opts.providers.filter((p) => p.name !== name)
    if (cfg) next.push(cfg)
    setOpts({ ...opts, providers: next })
  }

  return (
    <div className="space-y-4 pt-1">
      <div className="flex items-center gap-3">
        <Checkbox
          id="oauth2-enabled"
          checked={opts.enabled}
          onCheckedChange={(v) => setOpts({ ...opts, enabled: !!v })}
        />
        <Label htmlFor="oauth2-enabled" className="cursor-pointer">Enable OAuth2</Label>
      </div>

      {opts.enabled && (
        <div className="space-y-2">
          <p className="text-xs text-muted-foreground">Configure OAuth2 providers for this collection.</p>
          {PROVIDERS.map((name) => (
            <ProviderCard
              key={name}
              name={name}
              config={providerMap[name]}
              onChange={(cfg) => updateProvider(name, cfg)}
            />
          ))}

          <div className="pt-2 space-y-2">
            <p className="text-xs font-medium text-muted-foreground">Mapped fields</p>
            <div className="grid grid-cols-2 gap-3">
              {(['name', 'username', 'avatarURL'] as const).map((f) => (
                <div key={f} className="space-y-1">
                  <Label className="text-xs capitalize">{f}</Label>
                  <Input
                    className="h-8 text-xs"
                    placeholder={f}
                    value={opts.mappedFields[f] ?? ''}
                    onChange={(e) => setOpts({ ...opts, mappedFields: { ...opts.mappedFields, [f]: e.target.value } })}
                  />
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

export function CollectionTabs({
  type,
  fields,
  setFields,
  rules,
  setRules,
  viewQuery,
  setViewQuery,
  oauth2Options,
  setOAuth2Options,
}: CollectionTabsProps) {
  const [showPicker, setShowPicker] = useState(false)
  const { data: collections = [] } = useCollections()
  const oauth2 = oauth2Options ?? DEFAULT_OAUTH2

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
          Leave empty for public access. Use filter expressions (e.g. <code className="bg-muted px-1 rounded text-[11px]">user = @request.auth.id</code>) to restrict access.
        </p>
      </TabsContent>

      {type === 'auth' && (
        <TabsContent value="options">
          {setOAuth2Options ? (
            <OAuth2Tab opts={oauth2} setOpts={setOAuth2Options} />
          ) : (
            <p className="text-sm text-muted-foreground">Auth collection options will appear here.</p>
          )}
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
