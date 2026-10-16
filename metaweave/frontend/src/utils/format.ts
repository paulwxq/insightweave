export function formatSize(bytes: number): string {
  const gb = bytes / 1024 ** 3
  if (gb >= 0.01) return `${gb.toFixed(2)} GB`
  const mb = bytes / 1024 ** 2
  if (mb >= 0.01) return `${mb.toFixed(2)} MB`
  return `${(bytes / 1024).toFixed(2)} KB`
}
