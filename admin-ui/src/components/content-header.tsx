interface ContentHeaderProps {
  left: React.ReactNode
  right?: React.ReactNode
}

export function ContentHeader({ left, right }: ContentHeaderProps) {
  return (
    <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 px-4 py-2.5 md:px-6 md:py-3 border-b bg-white shrink-0">
      <div className="flex items-center gap-2 min-w-0">{left}</div>
      {right && (
        <div className="flex items-center gap-1.5 flex-shrink-0 flex-wrap">{right}</div>
      )}
    </div>
  )
}
