import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, queryString, request } from './client'
import type { ItemCreate, SourceCreate } from './types'
const manual: ItemCreate = { title: 'Материал', url: '', raw_text: 'Текст', type: 'news', run_llm: false, force: false }
const source: SourceCreate = { url: 'https://example.org/rss', title: 'Источник', type: 'rss', poll_interval: '1h', category_hint: null, backfill_limit: 20, created_by: '' }
beforeEach(() => vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { headers: { 'Content-Type': 'application/json' } }))))
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })
describe('HTTP endpoint contracts', () => {
  const cases: [string, string, () => Promise<unknown>, unknown?][] = [
    ['/processing', 'GET', () => api.processing()], ['/processing/runs?limit=20', 'GET', () => api.processingRuns()],
    ['/processing/runs/7', 'GET', () => api.processingRun(7)], ['/processing/runs', 'POST', () => api.startProcessing({ limit: 10, force: false }), { limit: 10, force: false }],
    ['/collection', 'GET', () => api.collection()], ['/collection/start', 'POST', () => api.startCollection(60), { interval_seconds: 60 }],
    ['/collection/stop', 'POST', () => api.stopCollection()], ['/collection/runs', 'POST', () => api.collect({ due_only: true, backfill: false, force: false }), { due_only: true, backfill: false, force: false }],
    ['/items/1/events', 'POST', () => api.addEvent(1, { status: 'слушания', note: 'Комментарий', source_url: '' }), { status: 'слушания', note: 'Комментарий', source_url: '' }],
    ['/items/1/archive', 'POST', () => api.archive(1)], ['/items/1/unarchive', 'POST', () => api.unarchive(1)],
    ['/health', 'GET', () => api.health()], ['/health/ready', 'GET', () => api.ready()], ['/filters', 'GET', () => api.filters()], ['/status', 'GET', () => api.status()],
    ['/items', 'GET', () => api.feed({})], ['/items/facets', 'GET', () => api.facets({})], ['/documents', 'GET', () => api.documents({})],
    ['/items/1', 'GET', () => api.card(1)], ['/items', 'POST', () => api.createItem(manual), manual],
    ['/items/1', 'PATCH', () => api.editItem(1, { title: 'Правка', tags: ['тренды'] }), { title: 'Правка', tags: ['тренды'] }],
    ['/items/1/hide', 'POST', () => api.hideItem(1, 'digest', 'адресный'), { scope: 'digest', reason: 'адресный' }],
    ['/items/1/unhide', 'POST', () => api.unhideItem(1)], ['/items/1', 'DELETE', () => api.deleteItem(1)], ['/items/1/restore', 'POST', () => api.restoreItem(1)],
    ['/items/bulk', 'POST', () => api.bulk([1, 2], 'visible', ''), { item_ids: [1, 2], scope: 'visible', reason: '' }],
    ['/items/1/revisions', 'GET', () => api.revisions(1)], ['/items/1/revert', 'POST', () => api.revert(1, 'summary'), { field: 'summary' }],
    ['/items/1/notes', 'POST', () => api.note(1, 'Заметка', 'Автор'), { body: 'Заметка', author: 'Автор' }],
    ['/sources', 'GET', () => api.sources()], ['/sources/1', 'GET', () => api.source(1)],
    ['/sources/probe', 'POST', () => api.probe(source.url), { url: source.url }], ['/sources', 'POST', () => api.createSource(source), source],
    ['/sources/1', 'PATCH', () => api.editSource(1, { title: 'Правка', status: 'paused' }), { title: 'Правка', status: 'paused' }],
    ['/sources/1?purge_items=true', 'DELETE', () => api.deleteSource(1, true)], ['/sources/1/restore', 'POST', () => api.restoreSource(1)],
    ['/sources/1/refresh', 'POST', () => api.refreshSource(1)], ['/sources/1/health?limit=20', 'GET', () => api.sourceHealth(1)],
    ['/digest', 'POST', () => api.digest({ type: 'npa', tag: ['тренды'] }, 'markdown', 'Неделя', true), { filters: { type: 'npa', tag: ['тренды'] }, format: 'markdown', title: 'Неделя', include_notes: true }],
  ]
  it.each(cases)('%s uses %s with the backend request schema', async (path, method, call, body) => {
    await call()
    expect(fetch).toHaveBeenCalledWith(`/api/v1${path}`, expect.objectContaining({ method, ...(body === undefined ? {} : { body: JSON.stringify(body) }) }))
    if (body !== undefined) expect(vi.mocked(fetch).mock.calls[0]![1]!.headers).toMatchObject({ 'Content-Type': 'application/json' })
  })
  it('encodes repeated filters and opaque cursors without changing Cyrillic search', () => {
    const params = new URLSearchParams(queryString({ q: 'ИИ & ЦБ', priority: ['high', 'medium'], source_id: [1, 2], tag: ['тренды'], cursor: 'a+/=', include_hidden: false, empty: undefined }))
    expect(params.getAll('priority')).toEqual(['high', 'medium']); expect(params.getAll('source_id')).toEqual(['1', '2'])
    expect(params.get('q')).toBe('ИИ & ЦБ'); expect(params.get('cursor')).toBe('a+/='); expect(params.has('empty')).toBe(false)
  })
})
describe('API failures and cancellation', () => {
  it('preserves the health schema on readiness HTTP 503', async () => {
    const health = { status: 'degraded', app: 'hub', version: '2', environment: 'local', checks: { repository: 'error' } }
    vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify(health), { status: 503 }))
    await expect(api.ready()).resolves.toEqual(health)
  })
  it.each([{ detail: 'Хранилище недоступно', code: 'repository_unavailable' }, { status: 'degraded', checks: null }, null])('does not swallow readiness failures without a valid health response: %j', async body => {
    vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify(body), { status: 503 }))
    await expect(api.ready()).rejects.toMatchObject({ status: 503 })
  })
  it.each([400, 404, 409, 422, 500, 503])('preserves backend problem details for HTTP %i', async status => {
    vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'Ошибка сервера', code: 'validation_error', details: { item_id: 1 } }), { status }))
    await expect(request('/items')).rejects.toMatchObject({ status, code: 'validation_error', message: 'Ошибка сервера', details: { item_id: 1 } })
  })
  it('reports a disconnected backend instead of returning demo data', async () => {
    vi.mocked(fetch).mockRejectedValueOnce(new TypeError('network'))
    await expect(api.feed({})).rejects.toThrow('Не удалось подключиться')
  })
  it.each(['<html>Vite page</html>', 'null', '"text"'])('rejects malformed success responses: %s', async body => {
    vi.mocked(fetch).mockResolvedValueOnce(new Response(body))
    await expect(api.feed({})).rejects.toBeInstanceOf(ApiError)
  })
  it('times out a hung request', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockImplementation((_url, options) => new Promise((_resolve, reject) => options?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))))
    const result = expect(request('/items', { timeout: 10 })).rejects.toThrow('не ответил вовремя')
    await vi.advanceTimersByTimeAsync(10); await result
  })
  it('propagates caller cancellation without displaying it as a server failure', async () => {
    const controller = new AbortController()
    vi.mocked(fetch).mockImplementation((_url, options) => new Promise((_resolve, reject) => options?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))))
    const result = expect(request('/items', { signal: controller.signal })).rejects.toMatchObject({ name: 'AbortError' })
    controller.abort(); await result
  })
})
