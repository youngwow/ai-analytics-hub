import { describe, expect, it, vi } from 'vitest'
import { downloadText, formatDate, normalizeSourceUrl, safeUrl, splitTags } from './dashboard'
describe('untrusted API display values', () => {
  it.each(['javascript:alert(1)', 'data:text/html,test', 'file:///etc/passwd', 'https://user:secret@example.org', 'invalid', null])('does not link unsafe original %s', url => { expect(safeUrl(url)).toBeUndefined() })
  it('preserves valid HTTP links and query values', () => { expect(safeUrl('https://example.org/?q=/')).toBe('https://example.org/?q=/'); expect(normalizeSourceUrl('example.org/rss')).toBe('https://example.org/rss') })
  it('handles missing and invalid dates without displaying Invalid Date', () => { expect(formatDate(null)).toBe('—'); expect(formatDate('invalid')).toBe('—'); expect(formatDate('2026-01-01T12:00:00Z')).toContain('2026') })
  it('normalizes custom tags without adding duplicate or empty values', () => { expect(splitTags(' ИИ, ,регуляторика,ИИ ')).toEqual(['ИИ', 'регуляторика']) })
  it('exports JSON as parseable UTF-8 without a byte-order mark', async () => {
    let exported: Blob | undefined
    const revoke = vi.fn()
    vi.useFakeTimers()
    vi.stubGlobal('URL', { createObjectURL: (blob: Blob) => { exported = blob; return 'blob:export' }, revokeObjectURL: revoke })
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    try {
      downloadText('{"title":"Обзор"}', 'digest.json', 'application/json;charset=utf-8')
      vi.advanceTimersByTime(1000); expect(revoke).toHaveBeenCalledWith('blob:export')
      vi.useRealTimers()
      const bytes = await new Promise<ArrayBuffer>((resolve, reject) => {
        const reader = new FileReader(); reader.onload = () => resolve(reader.result as ArrayBuffer); reader.onerror = reject; reader.readAsArrayBuffer(exported!)
      })
      expect(JSON.parse(new TextDecoder().decode(bytes))).toEqual({ title: 'Обзор' })
      expect(new Uint8Array(bytes)[0]).toBe(123)
    } finally { click.mockRestore(); vi.unstubAllGlobals(); vi.useRealTimers() }
  })
})
