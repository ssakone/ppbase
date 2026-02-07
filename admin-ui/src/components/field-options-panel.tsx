import { Field, Collection } from '@/api/types'
import { FIELD_TYPE_CONFIG } from '@/lib/field-types'
import { Label } from '@/components/ui/label'
import { Input } from '@/components/ui/input'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { TagsInput } from '@/components/tags-input'
import { Button } from '@/components/ui/button'

interface FieldOptionsPanelProps {
  field: Field
  onChange: (field: Field) => void
  collections: Collection[]
}

export function FieldOptionsPanel({ field, onChange, collections }: FieldOptionsPanelProps) {
  const opts = field.options || {}

  const getOpt = <T,>(key: string, flat?: T): T | undefined => {
    const flatVal = (field as unknown as Record<string, unknown>)[key] as T | undefined
    return flatVal ?? (opts as Record<string, unknown>)[key] as T | undefined ?? flat
  }

  const setOpt = (key: string, value: unknown) => {
    onChange({ ...field, [key]: value })
  }

  return (
    <div className="space-y-4 border-t pt-3 mt-2 px-1">
      <div className="flex items-center gap-2">
        <Checkbox
          id={`req-${field.name}-${field.type}`}
          checked={!!field.required}
          onCheckedChange={(checked) => onChange({ ...field, required: !!checked })}
        />
        <Label htmlFor={`req-${field.name}-${field.type}`} className="text-sm font-normal">
          Required
        </Label>
      </div>

      {renderTypeOptions(field.type, field, getOpt, setOpt, onChange, collections)}
    </div>
  )
}

function renderTypeOptions(
  type: string,
  field: Field,
  getOpt: <T>(key: string, fallback?: T) => T | undefined,
  setOpt: (key: string, value: unknown) => void,
  onChange: (field: Field) => void,
  collections: Collection[],
) {
  switch (type) {
    case 'text':
      return (
        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-1.5">
            <Label className="text-xs">Min length</Label>
            <Input
              type="number"
              min={0}
              placeholder="0"
              value={getOpt<number>('min') ?? ''}
              onChange={(e) => setOpt('min', e.target.value ? parseInt(e.target.value) : undefined)}
            />
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs">Max length</Label>
            <Input
              type="number"
              min={0}
              placeholder="5000"
              value={getOpt<number>('max') ?? ''}
              onChange={(e) => setOpt('max', e.target.value ? parseInt(e.target.value) : undefined)}
            />
          </div>
          <div className="col-span-2 space-y-1.5">
            <Label className="text-xs">
              Pattern <span className="text-muted-foreground">(regex)</span>
            </Label>
            <Input
              type="text"
              placeholder="e.g. ^[a-z]+$"
              value={getOpt<string>('pattern') ?? ''}
              onChange={(e) => setOpt('pattern', e.target.value || undefined)}
            />
          </div>
        </div>
      )

    case 'number':
      return (
        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-1.5">
            <Label className="text-xs">Min value</Label>
            <Input
              type="number"
              step="any"
              placeholder="No limit"
              value={getOpt<number>('min') ?? ''}
              onChange={(e) => setOpt('min', e.target.value !== '' ? parseFloat(e.target.value) : undefined)}
            />
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs">Max value</Label>
            <Input
              type="number"
              step="any"
              placeholder="No limit"
              value={getOpt<number>('max') ?? ''}
              onChange={(e) => setOpt('max', e.target.value !== '' ? parseFloat(e.target.value) : undefined)}
            />
          </div>
          <div className="col-span-2 flex items-center gap-2">
            <Checkbox
              id="onlyInt"
              checked={!!getOpt<boolean>('onlyInt')}
              onCheckedChange={(checked) => setOpt('onlyInt', !!checked || undefined)}
            />
            <Label htmlFor="onlyInt" className="text-sm font-normal">
              Integer only
            </Label>
          </div>
        </div>
      )

    case 'select':
      return (
        <div className="space-y-3">
          <div className="space-y-1.5">
            <Label className="text-xs">Values</Label>
            <TagsInput
              value={getOpt<string[]>('values') ?? []}
              onChange={(values) => setOpt('values', values)}
              placeholder="Type a value and press Enter"
            />
            <p className="text-xs text-muted-foreground">
              Press Enter or comma to add a value.
            </p>
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs">Max select</Label>
            <Select
              value={String(getOpt<number>('maxSelect') ?? 1)}
              onValueChange={(v) => setOpt('maxSelect', parseInt(v))}
            >
              <SelectTrigger className="w-[200px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="1">Single (1)</SelectItem>
                <SelectItem value="2">Multiple (2)</SelectItem>
                <SelectItem value="3">Multiple (3)</SelectItem>
                <SelectItem value="5">Multiple (5)</SelectItem>
                <SelectItem value="10">Multiple (10)</SelectItem>
                <SelectItem value="999">Unlimited</SelectItem>
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              Single allows only 1 value; multiple stores as array.
            </p>
          </div>
        </div>
      )

    case 'relation':
      return (
        <div className="space-y-3">
          <div className="space-y-1.5">
            <Label className="text-xs">Related collection</Label>
            <Select
              value={getOpt<string>('collectionId') ?? ''}
              onValueChange={(v) => setOpt('collectionId', v || undefined)}
            >
              <SelectTrigger>
                <SelectValue placeholder="-- Select a collection --" />
              </SelectTrigger>
              <SelectContent>
                {collections.map((c) => (
                  <SelectItem key={c.id} value={c.id}>
                    {c.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              The collection this field links to.
            </p>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label className="text-xs">Max select</Label>
              <Select
                value={String(getOpt<number>('maxSelect') ?? 1)}
                onValueChange={(v) => setOpt('maxSelect', parseInt(v))}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="1">Single (1)</SelectItem>
                  <SelectItem value="5">Multiple (5)</SelectItem>
                  <SelectItem value="10">Multiple (10)</SelectItem>
                  <SelectItem value="999">Unlimited</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="flex items-end pb-1">
              <div className="flex items-center gap-2">
                <Checkbox
                  id="cascadeDelete"
                  checked={!!getOpt<boolean>('cascadeDelete')}
                  onCheckedChange={(checked) => setOpt('cascadeDelete', !!checked || undefined)}
                />
                <Label htmlFor="cascadeDelete" className="text-sm font-normal">
                  Cascade delete
                </Label>
              </div>
            </div>
          </div>
        </div>
      )

    case 'file': {
      const mimeTypes = getOpt<string[]>('mimeTypes') ?? []
      return (
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label className="text-xs">Max file size</Label>
              <Select
                value={String(getOpt<number>('maxSize') ?? 5242880)}
                onValueChange={(v) => setOpt('maxSize', parseInt(v))}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="1048576">1 MB</SelectItem>
                  <SelectItem value="5242880">5 MB</SelectItem>
                  <SelectItem value="10485760">10 MB</SelectItem>
                  <SelectItem value="52428800">50 MB</SelectItem>
                  <SelectItem value="104857600">100 MB</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label className="text-xs">Max files</Label>
              <Select
                value={String(getOpt<number>('maxSelect') ?? 1)}
                onValueChange={(v) => setOpt('maxSelect', parseInt(v))}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="1">Single (1)</SelectItem>
                  <SelectItem value="5">Multiple (5)</SelectItem>
                  <SelectItem value="10">Multiple (10)</SelectItem>
                  <SelectItem value="99">Multiple (99)</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs">
              Allowed MIME types <span className="text-muted-foreground">(empty = all)</span>
            </Label>
            <TagsInput
              value={mimeTypes}
              onChange={(values) => setOpt('mimeTypes', values)}
              placeholder="e.g. image/jpeg, application/pdf"
            />
            <div className="flex gap-2 mt-1">
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-6 text-xs"
                onClick={() => {
                  const presets = ['image/jpeg', 'image/png', 'image/webp', 'image/gif']
                  const merged = [...new Set([...mimeTypes, ...presets])]
                  setOpt('mimeTypes', merged)
                }}
              >
                Images
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-6 text-xs"
                onClick={() => {
                  const presets = [
                    'application/pdf',
                    'application/msword',
                    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                  ]
                  const merged = [...new Set([...mimeTypes, ...presets])]
                  setOpt('mimeTypes', merged)
                }}
              >
                Documents
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-6 text-xs"
                onClick={() => {
                  const presets = ['video/mp4', 'video/webm']
                  const merged = [...new Set([...mimeTypes, ...presets])]
                  setOpt('mimeTypes', merged)
                }}
              >
                Videos
              </Button>
            </div>
          </div>
        </div>
      )
    }

    case 'email':
    case 'url':
      return (
        <div className="space-y-3">
          <div className="space-y-1.5">
            <Label className="text-xs">
              Only domains <span className="text-muted-foreground">(allowlist)</span>
            </Label>
            <Input
              type="text"
              placeholder="e.g. example.com, company.org"
              value={(getOpt<string[]>('onlyDomains') ?? []).join(', ')}
              onChange={(e) => {
                const val = e.target.value.trim()
                setOpt('onlyDomains', val ? val.split(',').map((d) => d.trim()).filter(Boolean) : undefined)
              }}
            />
            <p className="text-xs text-muted-foreground">Comma-separated. Leave empty to allow all.</p>
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs">
              Except domains <span className="text-muted-foreground">(blocklist)</span>
            </Label>
            <Input
              type="text"
              placeholder="e.g. spam.com, test.org"
              value={(getOpt<string[]>('exceptDomains') ?? []).join(', ')}
              onChange={(e) => {
                const val = e.target.value.trim()
                setOpt('exceptDomains', val ? val.split(',').map((d) => d.trim()).filter(Boolean) : undefined)
              }}
            />
            <p className="text-xs text-muted-foreground">Mutually exclusive with "only domains".</p>
          </div>
        </div>
      )

    case 'date':
      return (
        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-1.5">
            <Label className="text-xs">Min date</Label>
            <Input
              type="datetime-local"
              value={getOpt<string>('min') ?? ''}
              onChange={(e) => setOpt('min', e.target.value || undefined)}
            />
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs">Max date</Label>
            <Input
              type="datetime-local"
              value={getOpt<string>('max') ?? ''}
              onChange={(e) => setOpt('max', e.target.value || undefined)}
            />
          </div>
        </div>
      )

    case 'json':
      return (
        <div className="space-y-1.5">
          <Label className="text-xs">Max size (bytes)</Label>
          <Input
            type="number"
            min={1}
            placeholder="1048576"
            value={getOpt<number>('maxSize') ?? ''}
            onChange={(e) => setOpt('maxSize', e.target.value ? parseInt(e.target.value) : undefined)}
          />
          <p className="text-xs text-muted-foreground">Default: 1 MB (1048576 bytes).</p>
        </div>
      )

    case 'editor':
      return (
        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-1.5">
            <Label className="text-xs">Max size (bytes)</Label>
            <Input
              type="number"
              min={1}
              placeholder="5242880"
              value={getOpt<number>('maxSize') ?? ''}
              onChange={(e) => setOpt('maxSize', e.target.value ? parseInt(e.target.value) : undefined)}
            />
            <p className="text-xs text-muted-foreground">Default: 5 MB.</p>
          </div>
          <div className="flex items-end pb-1">
            <div className="flex items-center gap-2">
              <Checkbox
                id="convertURLs"
                checked={!!getOpt<boolean>('convertURLs')}
                onCheckedChange={(checked) => setOpt('convertURLs', !!checked || undefined)}
              />
              <Label htmlFor="convertURLs" className="text-sm font-normal">
                Convert URLs
              </Label>
            </div>
          </div>
        </div>
      )

    case 'bool':
    default:
      return null
  }
}
