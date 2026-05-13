export function SkeletonLine({ width = 'w-full', height = 'h-3', className = '' }) {
  return (
    <div className={`${width} ${height} bg-[#263044] rounded animate-pulse ${className}`} />
  );
}

export function SkeletonBox({ className = '' }) {
  return <div className={`bg-[#263044] rounded animate-pulse ${className}`} />;
}

export function SkeletonCard({ rows = 4 }) {
  return (
    <div className="space-y-3 p-4">
      {Array.from({ length: rows }, (_, i) => (
        <SkeletonLine key={i} width={i % 3 === 0 ? 'w-2/3' : 'w-full'} />
      ))}
    </div>
  );
}

export function SkeletonTable({ cols = 6, rows = 5 }) {
  return (
    <div className="p-4 space-y-2">
      {Array.from({ length: rows }, (_, r) => (
        <div key={r} className="flex gap-3">
          {Array.from({ length: cols }, (_, c) => (
            <SkeletonLine key={c} width="flex-1" height="h-4" />
          ))}
        </div>
      ))}
    </div>
  );
}
