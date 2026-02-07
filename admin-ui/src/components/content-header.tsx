interface ContentHeaderProps {
  left: React.ReactNode
  right?: React.ReactNode
}

export function ContentHeader({ left, right }: ContentHeaderProps) {
  return (
    <div className="flex items-center justify-between px-6 py-4 border-b bg-white">
      <div className="flex items-center gap-3">{left}</div>
      <div className="flex items-center gap-2">{right}</div>
    </div>
  )
}
