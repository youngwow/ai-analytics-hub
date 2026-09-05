export function safeUrl(value: string | null | undefined) {
  if (!value) return undefined
  try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : undefined } catch { return undefined }
}
export function normalizeSourceUrl(value: string) {
  const url = new URL(/^https?:\/\//i.test(value.trim()) ? value.trim() : `https://${value.trim()}`)
  if (!safeUrl(url.href) || !url.hostname.includes('.')) throw new Error('Укажите корректный HTTP(S) адрес источника.')
  return url.href
}
export function formatDate(value: string | null | undefined, time = false) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleString('ru-RU', { year: 'numeric', month: '2-digit', day: '2-digit', ...(time ? { hour: '2-digit', minute: '2-digit' } as const : {}) })
}
export function downloadText(body: string, filename: string, type = 'text/plain;charset=utf-8') {
  const url = URL.createObjectURL(new Blob(type.startsWith('application/json') ? [body] : ['\uFEFF', body], { type }))
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = filename; anchor.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
export const splitTags = (value: string) => [...new Set(value.split(',').map(tag => tag.trim()).filter(Boolean))]
