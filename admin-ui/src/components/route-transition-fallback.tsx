export function RouteTransitionFallback() {
  return (
    <div className="flex-1 overflow-auto p-4 md:p-6 animate-fade-in">
      <div className="mx-auto max-w-7xl space-y-4">
        <div className="h-16 rounded-xl skeleton-shimmer" />
        <div className="h-40 rounded-xl skeleton-shimmer" />
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
          {[1, 2, 3, 4].map((idx) => (
            <div key={idx} className="h-28 rounded-xl skeleton-shimmer" />
          ))}
        </div>
      </div>
    </div>
  )
}
