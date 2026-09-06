import type { CollectionStatus, CollectionRunRequest, ProcessingStatus, ProcessingRun, ProcessingRunRequest, NpaEventCreate, Digest, Documents, Facets, Feed, FeedQuery, Filters, Health, Item, ItemCard, ItemCreate, ItemUpdate, ManualResult, Note, Probe, Revision, Source, SourceCreate, SourceHealth, SourceRun, SourceUpdate, Status, Visibility } from './types'

export class ApiError extends Error {
  constructor(message: string, public status = 0, public code = '', public details?: Record<string, unknown>) { super(message); this.name = 'ApiError' }
}
export function queryString(query: object = {}) {
  const params = new URLSearchParams()
  Object.entries(query).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return
    for (const entry of Array.isArray(value) ? value : [value]) params.append(key, String(entry))
  })
  return params.size ? `?${params}` : ''
}
export const apiBase = (import.meta.env.VITE_API_BASE_URL || '/api/v1').replace(/\/$/, '')
export async function request<T>(path: string, options: { method?: string; body?: unknown; query?: object; signal?: AbortSignal; timeout?: number; acceptDegradedHealth?: boolean } = {}): Promise<T> {
  const controller = new AbortController()
  const abort = () => controller.abort()
  if (options.signal?.aborted) abort()
  else options.signal?.addEventListener('abort', abort, { once: true })
  const timer = setTimeout(abort, options.timeout ?? 15000)
  try {
    const response = await fetch(`${apiBase}${path}${queryString(options.query)}`, {
      method: options.method ?? 'GET', signal: controller.signal,
      headers: { Accept: 'application/json', ...(options.body === undefined ? {} : { 'Content-Type': 'application/json' }) },
      ...(options.body === undefined ? {} : { body: JSON.stringify(options.body) }),
    })
    if (response.status === 204) return undefined as T
    let body: unknown
    try { body = await response.json() } catch {
      throw new ApiError(response.ok ? 'Сервер вернул неверный формат данных. Проверьте подключение API.' : `Ошибка сервера (${response.status}).`, response.status)
    }
    if (!response.ok) {
      if (options.acceptDegradedHealth && response.status === 503 && isHealth(body) && body.status === 'degraded') return body as T
      const problem = (body && typeof body === 'object' ? body : {}) as Record<string, unknown>
      throw new ApiError(typeof problem.detail === 'string' ? problem.detail : typeof problem.title === 'string' ? problem.title : `Ошибка запроса (${response.status}).`, response.status,
        typeof problem.code === 'string' ? problem.code : '', problem.details && typeof problem.details === 'object' ? problem.details as Record<string, unknown> : undefined)
    }
    if (body === null || typeof body !== 'object') throw new ApiError('Сервер вернул неверный формат данных.', response.status)
    return body as T
  } catch (error) {
    if (error instanceof ApiError) throw error
    if (options.signal?.aborted) throw new DOMException('Отменено', 'AbortError')
    throw new ApiError(controller.signal.aborted ? 'Сервер не ответил вовремя. Попробуйте ещё раз.' : 'Не удалось подключиться к серверу. Проверьте, запущен ли backend.')
  } finally { clearTimeout(timer); options.signal?.removeEventListener('abort', abort) }
}
function isHealth(value: unknown): value is Health {
  if (!value || typeof value !== 'object') return false
  const body = value as Record<string, unknown>
  return ['ok', 'degraded'].includes(String(body.status)) && typeof body.app === 'string' && typeof body.version === 'string' && typeof body.environment === 'string'
    && !!body.checks && typeof body.checks === 'object' && !Array.isArray(body.checks) && Object.values(body.checks).every(check => typeof check === 'string')
}
const post = <T>(path: string, body?: unknown, timeout?: number) => request<T>(path, { method: 'POST', body, timeout })
export const api = {
  processing: () => request<ProcessingStatus>('/processing'),
  processingRuns: () => request<{ runs: ProcessingRun[] }>('/processing/runs', { query: { limit: 20 } }),
  processingRun: (id: number) => request<ProcessingRun>(`/processing/runs/${id}`),
  startProcessing: (body: ProcessingRunRequest) => post<ProcessingRun>('/processing/runs', body),
  collection: () => request<CollectionStatus>('/collection'),
  startCollection: (interval_seconds: number) => post<CollectionStatus>('/collection/start', { interval_seconds }),
  stopCollection: () => post<CollectionStatus>('/collection/stop'),
  collect: (body: CollectionRunRequest) => post<CollectionStatus>('/collection/runs', body),
  addEvent: (id: number, body: NpaEventCreate) => post<ItemCard['events'][number]>(`/items/${id}/events`, body),
  archive: (id: number) => post<{ id: number; is_archived: boolean }>(`/items/${id}/archive`),
  unarchive: (id: number) => post<{ id: number; is_archived: boolean }>(`/items/${id}/unarchive`),
  health: () => request<Health>('/health'), ready: () => request<Health>('/health/ready', { acceptDegradedHealth: true }),
  filters: () => request<Filters>('/filters'), status: () => request<Status>('/status'),
  feed: (query: FeedQuery, signal?: AbortSignal) => request<Feed>('/items', { query, signal }),
  facets: (query: FeedQuery, signal?: AbortSignal) => request<Facets>('/items/facets', { query, signal }),
  documents: (query: FeedQuery, signal?: AbortSignal) => request<Documents>('/documents', { query, signal }),
  card: (id: number) => request<ItemCard>(`/items/${id}`),
  createItem: (body: ItemCreate) => post<ManualResult>('/items', body, 120000),
  editItem: (id: number, body: ItemUpdate) => request<{ item: Item; manual_overrides: string[] }>(`/items/${id}`, { method: 'PATCH', body }),
  hideItem: (id: number, scope: 'feed' | 'digest', reason: string) => post<{ id: number; visibility: Visibility }>(`/items/${id}/hide`, { scope, reason }),
  unhideItem: (id: number) => post<{ id: number; visibility: Visibility }>(`/items/${id}/unhide`),
  deleteItem: (id: number) => request<{ id: number; visibility: Visibility }>(`/items/${id}`, { method: 'DELETE' }),
  restoreItem: (id: number) => post<{ id: number; visibility: Visibility }>(`/items/${id}/restore`),
  bulk: (item_ids: number[], scope: 'feed' | 'digest' | 'visible', reason: string) => post<{ changed: number; scope: string }>('/items/bulk', { item_ids, scope, reason }),
  revisions: (id: number) => request<{ revisions: Revision[] }>(`/items/${id}/revisions`),
  revert: (id: number, field: string) => post<{ item: Item; manual_overrides: string[] }>(`/items/${id}/revert`, { field }),
  merge: (id: number, item_ids: number[], reason = '') => post<{ item: Item; absorbed: number[]; sources_count: number }>(`/items/${id}/merge`, { item_ids, reason }),
  notDuplicate: (id: number) => post<{ id: number; dismissed: number[] }>(`/items/${id}/not-duplicate`),
  note: (id: number, body: string, author: string) => post<Note>(`/items/${id}/notes`, { body, author }),
  sources: (query: { status?: string; kind?: string } = {}, signal?: AbortSignal) => request<{ sources: Source[] }>('/sources', { query, signal }),
  source: (id: number) => request<Source>(`/sources/${id}`),
  probe: (url: string) => post<Probe>('/sources/probe', { url }, 60000),
  createSource: (body: SourceCreate) => post<Source>('/sources', body, 60000),
  editSource: (id: number, body: SourceUpdate) => request<Source>(`/sources/${id}`, { method: 'PATCH', body, timeout: 60000 }),
  deleteSource: (id: number, purge_items: boolean) => request<{ source_id: number; name: string; documents_kept: number; items_hidden: number; tracked_npa: number }>(`/sources/${id}`, { method: 'DELETE', query: { purge_items } }),
  restoreSource: (id: number) => post<Source>(`/sources/${id}/restore`),
  refreshSource: (id: number) => post<SourceRun>(`/sources/${id}/refresh`, undefined, 120000),
  sourceHealth: (id: number) => request<SourceHealth>(`/sources/${id}/health`, { query: { limit: 20 } }),
  digest: (filters: FeedQuery, format: 'markdown' | 'json', title: string, include_notes: boolean) => post<Digest>('/digest', { filters, format, title, include_notes }),
}
