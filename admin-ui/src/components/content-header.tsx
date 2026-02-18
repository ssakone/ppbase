interface ContentHeaderProps {
  left: React.ReactNode
  right?: React.ReactNode
}

export function ContentHeader({ left, right }: ContentHeaderProps) {
  return (
    <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 px-6 py-4 md:px-8 md:py-4.5 border-b bg-white shrink-0 shadow-sm">
      <div className="flex items-center gap-3 min-w-0">{left}</div>
      {right && (
        <div className="flex items-center gap-2.5 flex-shrink-0 flex-wrap">{right}</div>
      )}
    </div>
  )
}
