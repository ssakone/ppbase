import { useState } from 'react'
import { Collection } from '@/api/types'
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from '@/components/ui/table'
import { cn } from '@/lib/utils'

interface ApiPreviewDrawerProps {
  open: boolean
  onClose: () => void
  collection: Collection
}

interface Endpoint {
  id: string
  label: string
  title: string
  description: string
  method: string
  methodClass: string
  path: string
  note?: string
  params: { name: string; type: string; desc: string }[]
  code: string
}

function buildEndpoints(name: string): Endpoint[] {
  return [
    {
      id: 'list',
      label: 'List/Search',
      title: `List/Search (${name})`,
      description: `Fetch a paginated <strong>${name}</strong> records list, supporting sorting and filtering.`,
      method: 'GET',
      methodClass: 'bg-green-100 text-green-700',
      path: `/api/collections/${name}/records`,
      note: 'Requires superuser <code>Authorization:TOKEN</code> header',
      params: [
        { name: 'page', type: 'Number', desc: 'The page (aka. offset) of the paginated list (default to 1).' },
        { name: 'perPage', type: 'Number', desc: 'Specify the max returned records per page (default to 30).' },
        { name: 'sort', type: 'String', desc: 'Specify the records order attribute(s). Add - / + (default) in front of the attribute for DESC and ASC order.' },
        { name: 'filter', type: 'String', desc: 'Filter expression to filter/search the returned records list.' },
        { name: 'expand', type: 'String', desc: 'Auto expand record relations.' },
      ],
      code: `<span class="hl-comment">// fetch a paginated records list</span>
<span class="hl-keyword">const</span> <span class="hl-var">resultList</span> = <span class="hl-keyword">await</span> pb.<span class="hl-func">collection</span>(<span class="hl-string">'${name}'</span>).<span class="hl-func">getList</span>(1, 50, {
    filter: <span class="hl-string">'someField1 != someField2'</span>,
});

<span class="hl-comment">// you can also fetch all records at once via getFullList</span>
<span class="hl-keyword">const</span> <span class="hl-var">records</span> = <span class="hl-keyword">await</span> pb.<span class="hl-func">collection</span>(<span class="hl-string">'${name}'</span>).<span class="hl-func">getFullList</span>({
    sort: <span class="hl-string">'-someField'</span>,
});

<span class="hl-comment">// or fetch only the first record that matches the specified filter</span>
<span class="hl-keyword">const</span> <span class="hl-var">record</span> = <span class="hl-keyword">await</span> pb.<span class="hl-func">collection</span>(<span class="hl-string">'${name}'</span>).<span class="hl-func">getFirstListItem</span>(<span class="hl-string">'someField="test"'</span>, {
    expand: <span class="hl-string">'relField1,relField2.subRelField'</span>,
});`,
    },
    {
      id: 'view',
      label: 'View',
      title: `View (${name})`,
      description: `Fetch a single <strong>${name}</strong> record by its ID.`,
      method: 'GET',
      methodClass: 'bg-green-100 text-green-700',
      path: `/api/collections/${name}/records/:id`,
      params: [
        { name: 'expand', type: 'String', desc: 'Auto expand record relations.' },
      ],
      code: `<span class="hl-keyword">const</span> <span class="hl-var">record</span> = <span class="hl-keyword">await</span> pb.<span class="hl-func">collection</span>(<span class="hl-string">'${name}'</span>).<span class="hl-func">getOne</span>(<span class="hl-string">'RECORD_ID'</span>, {
    expand: <span class="hl-string">'relField1,relField2.subRelField'</span>,
});`,
    },
    {
      id: 'create',
      label: 'Create',
      title: `Create (${name})`,
      description: `Create a new <strong>${name}</strong> record.`,
      method: 'POST',
      methodClass: 'bg-blue-100 text-blue-700',
      path: `/api/collections/${name}/records`,
      params: [],
      code: `<span class="hl-keyword">const</span> <span class="hl-var">record</span> = <span class="hl-keyword">await</span> pb.<span class="hl-func">collection</span>(<span class="hl-string">'${name}'</span>).<span class="hl-func">create</span>({
    <span class="hl-comment">// ... your data</span>
});`,
    },
    {
      id: 'update',
      label: 'Update',
      title: `Update (${name})`,
      description: `Update an existing <strong>${name}</strong> record.`,
      method: 'PATCH',
      methodClass: 'bg-amber-100 text-amber-700',
      path: `/api/collections/${name}/records/:id`,
      params: [],
      code: `<span class="hl-keyword">const</span> <span class="hl-var">record</span> = <span class="hl-keyword">await</span> pb.<span class="hl-func">collection</span>(<span class="hl-string">'${name}'</span>).<span class="hl-func">update</span>(<span class="hl-string">'RECORD_ID'</span>, {
    <span class="hl-comment">// ... your data</span>
});`,
    },
    {
      id: 'delete',
      label: 'Delete',
      title: `Delete (${name})`,
      description: `Delete a single <strong>${name}</strong> record.`,
      method: 'DELETE',
      methodClass: 'bg-red-100 text-red-700',
      path: `/api/collections/${name}/records/:id`,
      params: [],
      code: `<span class="hl-keyword">await</span> pb.<span class="hl-func">collection</span>(<span class="hl-string">'${name}'</span>).<span class="hl-func">delete</span>(<span class="hl-string">'RECORD_ID'</span>);`,
    },
  ]
}

export function ApiPreviewDrawer({ open, onClose, collection }: ApiPreviewDrawerProps) {
  const [activeTab, setActiveTab] = useState('list')
  const endpoints = buildEndpoints(collection.name)
  const activeEndpoint = endpoints.find((ep) => ep.id === activeTab) || endpoints[0]

  return (
    <Sheet open={open} onOpenChange={(o) => !o && onClose()}>
      <SheetContent side="right" className="w-full sm:max-w-4xl p-0 flex flex-col">
        <SheetHeader className="px-6 pt-6 pb-4 border-b">
          <SheetTitle>API Preview</SheetTitle>
        </SheetHeader>

        <div className="flex flex-1 overflow-hidden">
          {/* Left navigation */}
          <div className="w-40 shrink-0 border-r bg-muted/30 py-2">
            {endpoints.map((ep) => (
              <button
                key={ep.id}
                className={cn(
                  'w-full text-left px-4 py-2 text-sm transition-colors mb-1',
                  activeTab === ep.id
                    ? 'bg-muted font-medium text-foreground'
                    : 'text-muted-foreground hover:text-foreground hover:bg-muted',
                )}
                onClick={() => setActiveTab(ep.id)}
              >
                {ep.label}
              </button>
            ))}
          </div>

          {/* Content */}
          <div className="flex-1 overflow-y-auto p-6 space-y-6">
            <div>
              <h3 className="text-base font-semibold">{activeEndpoint.title}</h3>
              <p
                className="text-sm text-muted-foreground mt-1"
                dangerouslySetInnerHTML={{ __html: activeEndpoint.description }}
              />
            </div>

            {/* Code block */}
            <div className="rounded-lg bg-slate-950 text-slate-50 p-4 text-sm font-mono overflow-x-auto">
              <pre>
                <code dangerouslySetInnerHTML={{ __html: activeEndpoint.code }} />
              </pre>
            </div>
            <p className="text-xs text-muted-foreground">JavaScript SDK</p>

            {/* API details */}
            <div>
              <h4 className="text-sm font-semibold mb-2">API details</h4>
              <div className="flex items-center gap-2 rounded-lg border p-3 bg-muted/30">
                <Badge
                  variant="outline"
                  className={cn('font-mono text-xs font-bold', activeEndpoint.methodClass)}
                >
                  {activeEndpoint.method}
                </Badge>
                <code className="text-sm font-mono">{activeEndpoint.path}</code>
              </div>
              {activeEndpoint.note && (
                <p
                  className="text-xs text-muted-foreground mt-2"
                  dangerouslySetInnerHTML={{ __html: activeEndpoint.note }}
                />
              )}
            </div>

            {/* Query parameters */}
            {activeEndpoint.params.length > 0 && (
              <div>
                <h4 className="text-sm font-semibold mb-2">Query parameters</h4>
                <div className="rounded-lg border">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Param</TableHead>
                        <TableHead>Type</TableHead>
                        <TableHead>Description</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {activeEndpoint.params.map((p) => (
                        <TableRow key={p.name}>
                          <TableCell className="font-medium">{p.name}</TableCell>
                          <TableCell>
                            <Badge variant="secondary" className="text-xs">
                              {p.type}
                            </Badge>
                          </TableCell>
                          <TableCell
                            className="text-sm text-muted-foreground"
                            dangerouslySetInnerHTML={{ __html: p.desc }}
                          />
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              </div>
            )}
          </div>
        </div>
      </SheetContent>
    </Sheet>
  )
}
