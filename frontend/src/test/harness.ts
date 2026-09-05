import { beforeEach, afterEach, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { nextTick } from 'vue'
import App from '../App.vue'
import { card, filters, health, item, source, status, processing, processingRun, collection } from './fixtures'
export interface Call { url: URL; method: string; body: Record<string, unknown> | undefined; signal: AbortSignal | null | undefined }
export type Handler = (call: Call) => Response | Promise<Response> | undefined
export const json = (body: unknown, code = 200) => new Response(JSON.stringify(body), { status: code, headers: { 'Content-Type': 'application/json' } })
export function harness() {
  let wrapper: VueWrapper | undefined
  let handler: Handler | undefined
  const calls: Call[] = []
  let currentCard = structuredClone(card)
  beforeEach(() => {
    calls.length = 0; handler = undefined; currentCard = structuredClone(card)
    localStorage.clear(); history.replaceState(null, '', '/'); document.documentElement.classList.remove('dark')
    Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value: function (this: HTMLDialogElement) { this.setAttribute('open', '') } })
    vi.stubGlobal('fetch', vi.fn(async (input: string, init: RequestInit = {}) => {
      const call: Call = { url: new URL(input, 'http://localhost'), method: init.method || 'GET', body: init.body ? JSON.parse(String(init.body)) : undefined, signal: init.signal }
      calls.push(call)
      const custom = handler?.(call); if (custom !== undefined) return custom
      const path = call.url.pathname.replace('/api/v1', '')
      if (path === '/filters') return json(filters)
      if (path === '/status') return json(status)
      if (path.startsWith('/health')) return json(health)
      if (path === '/items/facets') return json({ total: 1, by_priority: { high: 1 }, by_type: { news: 1 }, by_source: [], top_tags: [], took_ms: 1 })
      if (path === '/items' && call.method === 'GET') return json({ items: [item], total: 1, next_cursor: null, took_ms: 1 })
      if (path === '/items' && call.method === 'POST') return json({ id: 102, document_id: 2, origin: 'manual', processing_status: 'done' }, 201)
      if (path === '/items/bulk') return json({ changed: (call.body?.item_ids as number[]).length, scope: call.body?.scope })
      if (path === '/items/101' && call.method === 'PATCH') { Object.assign(currentCard.item, call.body); return json({ item: currentCard.item, manual_overrides: ['title'] }) }
      if (path === '/items/101' && call.method === 'DELETE') { currentCard.item.visibility = 'deleted'; return json({ id: 101, visibility: 'deleted' }) }
      if (path === '/items/101') return json(currentCard)
      if (path === '/items/101/revisions') return json({ revisions: [] })
      if (path === '/items/101/notes') return json({ item_id: 101, ...call.body, id: 1, created_at: '2026-01-01' }, 201)
      if (path === '/items/101/revert') return json({ item: currentCard.item, manual_overrides: [] })
      if (path === '/items/101/hide') { currentCard.item.visibility = call.body?.scope === 'feed' ? 'hidden_feed' : 'hidden_digest'; return json({ id: 101, visibility: currentCard.item.visibility }) }
      if (path === '/items/101/unhide' || path === '/items/101/restore') { currentCard.item.visibility = 'visible'; return json({ id: 101, visibility: 'visible' }) }
      if (path === '/items/101/events') { const event = { id: 1, item_id: 101, ...call.body, created_at: '2026-01-01', created_by: 'user' }; currentCard.events.push(event as typeof currentCard.events[number]); return json(event, 201) }
      if (path === '/items/101/archive' || path === '/items/101/unarchive') { currentCard.item.is_archived = path.endsWith('/archive'); return json({ id: 101, is_archived: currentCard.item.is_archived }) }
      if (path === '/processing') return json(processing)
      if (path === '/processing/runs' && call.method === 'POST') return json(processingRun, 202)
      if (path === '/processing/runs') return json({ runs: [] })
      if (path === '/processing/runs/7') return json(processingRun)
      if (path.startsWith('/collection')) return json(collection, path.endsWith('/runs') ? 202 : 200)
      if (path === '/documents') return json({ documents: [], total: 0, next_cursor: null, took_ms: 1 })
      if (path === '/sources' && call.method === 'GET') return json({ sources: [source] })
      if (path === '/sources' && call.method === 'POST') return json({ ...source, id: 2, name: call.body?.title }, 201)
      if (path === '/sources/probe') return json({ resolved_type: 'rss', feed_url: source.url, title: source.name, detection_method: 'feed', suggested_poll_interval: '1h', already_exists: false, already_exists_source_id: null, preview: [], warnings: [], note: '' })
      if (path === '/sources/1/health') return json({ source, documents: 1, consecutive_failures: 0, last_success_at: null, last_error: null, runs: [] })
      if (path === '/sources/1/refresh') return json({ id: 1, source_id: 1, started_at: '2026-01-01', finished_at: '2026-01-01', http_status: 200, items_found: 1, items_new: 0, error_code: '', error_message: '' })
      if (path === '/sources/1' && call.method === 'DELETE') return json({ source_id: 1, name: source.name, documents_kept: 1, items_hidden: 0, tracked_npa: 0 })
      if (path === '/sources/1' || path === '/sources/1/restore') return json({ ...source, ...call.body })
      if (path === '/digest') return json({ title: 'Дайджест сервера', generated_at: '2026-01-01T00:00:00Z', items: 1, format: call.body?.format || 'markdown', body: call.body?.format === 'json' ? '{"items":[]}' : '# Дайджест сервера\nСаммари сервера' })
      throw new Error(`Unexpected API request: ${call.method} ${path}`)
    }))
  })
  afterEach(() => { wrapper?.unmount(); wrapper = undefined; document.body.innerHTML = ''; vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
  async function launch(hash = '') { if (hash) history.replaceState(null, '', hash); wrapper = mount(App, { attachTo: document.body }); await settle(); return wrapper }
  const app = () => wrapper!
  async function settle() { await flushPromises(); await nextTick() }
  function button(text: string) {
    const found = [...document.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent?.trim() === text)
    if (!found) throw new Error(`Missing button: ${text}`)
    return found
  }
  async function click(text: string) { button(text).click(); await settle() }
  async function field(label: string, value: string | boolean, scope: Element = document.body) {
    const element = [...scope.querySelectorAll('label')].find(node => node.childNodes[0]?.textContent?.trim() === label)?.querySelector('input,textarea,select') as HTMLInputElement | undefined
    if (!element) throw new Error(`Missing field: ${label}`)
    if (typeof value === 'boolean') element.checked = value
    else element.value = value
    element.dispatchEvent(new Event(element.tagName === 'SELECT' || element.type === 'checkbox' ? 'change' : 'input', { bubbles: true }))
    if (element.type === 'date') element.dispatchEvent(new Event('change', { bubbles: true }))
    await settle()
  }
  async function submit() { document.querySelector('dialog form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); await settle() }
  return { launch, app, settle, button, click, field, submit, calls, handle: (value: Handler) => { handler = value } }
}
