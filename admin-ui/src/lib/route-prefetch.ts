export type PrefetchKey =
  | 'dashboard'
  | 'collections'
  | 'records'
  | 'migrations'
  | 'logs'
  | 'settings'

const prefetchLoaders: Record<PrefetchKey, () => Promise<unknown>> = {
  dashboard: () => import('@/routes/dashboard'),
  collections: () => import('@/routes/collections/index'),
  records: () => import('@/routes/collections/[id]'),
  migrations: () => import('@/routes/migrations'),
  logs: () => import('@/routes/logs'),
  settings: () => import('@/routes/settings'),
}

const loaded = new Set<PrefetchKey>()

export function prefetchRoute(key: PrefetchKey): void {
  if (loaded.has(key)) {
    return
  }

  loaded.add(key)
  void prefetchLoaders[key]().catch(() => {
    loaded.delete(key)
  })
}

export function prefetchRoutes(keys: PrefetchKey[]): void {
  for (const key of keys) {
    prefetchRoute(key)
  }
}
