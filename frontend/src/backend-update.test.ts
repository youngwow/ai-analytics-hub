import { describe, expect, it, vi } from 'vitest'
import { harness, json } from './test/harness'
import { card, collection, processing, processingRun, source } from './test/fixtures'
const h = harness()
const calls = (path: string, method = 'POST') => h.calls.filter(call => call.url.pathname === `/api/v1${path}` && call.method === method)
const last = (path: string, method = 'POST') => calls(path, method).slice(-1)[0]!
const dialog = () => document.querySelector('dialog')!
async function openCard() { await h.click('Карточка и редактирование') }

describe('archive and events', () => {
  it('archives and restores independently of visibility and can search the archive', async () => {
    await h.launch(); await h.field('Архив', 'only')
    expect(last('/items', 'GET').url.searchParams.get('archived')).toBe('only')
    await openCard(); await h.click('В архив'); expect(calls('/items/101/archive')).toHaveLength(1)
    expect(dialog().textContent).toContain('Материал в архиве')
    await h.click('Вернуть из архива'); expect(calls('/items/101/unarchive')).toHaveLength(1)
    expect(calls('/items/101/hide')).toHaveLength(0)
  })
  it('creates a dated event and reloads the server chronology', async () => {
    await h.launch(); await openCard(); await h.click('Добавить событие / срок')
    await h.field('Статус или событие', 'Слушания', dialog()); await h.field('Дата события', '2026-09-10T10:00', dialog())
    await h.field('Ссылка события', 'https://example.org/event', dialog()); await h.field('Описание события', 'Обсудить поправки', dialog())
    await h.click('Сохранить событие')
    expect(last('/items/101/events').body).toEqual({ status: 'Слушания', occurred_at: new Date('2026-09-10T10:00').toISOString(), source_url: 'https://example.org/event', note: 'Обсудить поправки' })
    expect(dialog().textContent).toContain('Обсудить поправки'); expect(calls('/items/101', 'PATCH')).toHaveLength(0)
  })
  it('preserves event drafts after rejection and blocks unsafe event links', async () => {
    h.handle(call => call.url.pathname.endsWith('/events') ? json({ detail: 'Дата отклонена' }, 400) : undefined)
    await h.launch(); await openCard(); await h.click('Добавить событие / срок'); await h.field('Статус или событие', 'Срок', dialog())
    await h.field('Ссылка события', 'javascript:alert(1)', dialog()); await h.click('Сохранить событие'); expect(calls('/items/101/events')).toHaveLength(0)
    await h.field('Ссылка события', '', dialog()); await h.click('Сохранить событие'); expect(dialog().textContent).toContain('Дата отклонена')
    expect(dialog().querySelector<HTMLInputElement>('[list="event-statuses"]')!.value).toBe('Срок')
  })
  it('does not show successful archiving after a backend failure', async () => {
    h.handle(call => call.url.pathname.endsWith('/archive') ? json({ detail: 'Архив недоступен' }, 503) : undefined)
    await h.launch(); await openCard(); await h.click('В архив')
    expect(dialog().textContent).toContain('Архив недоступен'); expect(dialog().textContent).toContain('Материал не в архиве')
  })
})
describe('source relocation', () => {
  it('changes URL and type without resending the old fetch URL or schedule', async () => {
    await h.launch('#sources'); await h.click(source.name); await h.field('URL источника', 'https://example.org/new', dialog()); await h.field('Тип', 'html', dialog()); await h.submit()
    expect(last('/sources/1', 'PATCH').body).toEqual({ url: 'https://example.org/new', type: 'html' })
  })
  it('allows an explicit fetch address and keeps the form open on a duplicate conflict', async () => {
    h.handle(call => call.method === 'PATCH' ? json({ detail: 'Адрес занят источником #2', code: 'source_exists' }, 409) : undefined)
    await h.launch('#sources'); await h.click(source.name); await h.field('Адрес сбора', 'https://example.org/new.xml', dialog()); await h.submit()
    expect(last('/sources/1', 'PATCH').body).toEqual({ fetch_url: 'https://example.org/new.xml' }); expect(dialog().textContent).toContain('Адрес занят')
  })
})
describe('document pagination', () => {
  it('appends cursor pages and resets the cursor on filter changes', async () => {
    h.handle(call => call.url.pathname === '/api/v1/documents' ? json({ documents: [{ id: call.url.searchParams.has('cursor') ? 2 : 1, title: 'Документ', url: '', source_id: 1, source_name: 'Источник', published_at: null, chars: 10 }], total: 2, next_cursor: call.url.searchParams.has('cursor') ? null : 'doc+/=', took_ms: 1 }) : undefined)
    await h.launch('#status'); await h.click('Загрузить ещё документы')
    expect(last('/documents', 'GET').url.searchParams.get('cursor')).toBe('doc+/=')
    expect(h.app().findAll('table[aria-label="Необработанные документы"] tbody tr')).toHaveLength(2)
    await h.field('Источник', '1'); expect(last('/documents', 'GET').url.searchParams.has('cursor')).toBe(false)
    expect(h.app().findAll('table[aria-label="Необработанные документы"] tbody tr')).toHaveLength(1)
  })
})
describe('processing and collection', () => {
  it('submits processing options and reports acceptance rather than completion', async () => {
    await h.launch('#status'); await h.field('Лимит обработки', '12'); await h.field('Источник обработки', '1')
    await h.click('Обработать очередь ИИ')
    expect(last('/processing/runs').body).toEqual({ limit: 12, source_id: 1, force: false })
    expect(h.app().text()).toContain('Запрос на обработку принят'); expect(h.app().text()).toContain('Модель недоступна')
  })
  it('prevents launching a second run and exposes existing run details', async () => {
    h.handle(call => call.url.pathname === '/api/v1/processing' ? json({ ...processing, running: processingRun }) : call.url.pathname === '/api/v1/processing/runs' ? json({ runs: [processingRun] }) : undefined)
    await h.launch('#status'); expect(h.button('Обработать очередь ИИ').matches(':disabled')).toBe(true)
    await h.click('Прогон #7'); expect(calls('/processing/runs/7', 'GET')).toHaveLength(1); expect(dialog().textContent).toContain('Выполняется')
  })
  it('handles a concurrent processing conflict without claiming acceptance', async () => {
    h.handle(call => call.method === 'POST' && call.url.pathname === '/api/v1/processing/runs' ? json({ code: 'processing_busy', detail: 'Обработка уже идёт' }, 409) : undefined)
    await h.launch('#status'); await h.click('Обработать очередь ИИ')
    expect(h.app().text()).toContain('Обработка уже идёт'); expect(h.app().text()).not.toContain('Запрос на обработку принят')
  })
  it('starts and stops monitoring using server state', async () => {
    let running = false
    h.handle(call => {
      if (!call.url.pathname.startsWith('/api/v1/collection')) return undefined
      if (call.url.pathname.endsWith('/start')) running = true
      if (call.url.pathname.endsWith('/stop')) running = false
      return json({ ...collection, running })
    })
    await h.launch('#status'); await h.field('Интервал мониторинга, секунд', '60'); await h.click('Запустить автоматический мониторинг')
    expect(last('/collection/start').body).toEqual({ interval_seconds: 60 }); expect(h.app().text()).toContain('Мониторинг включён')
    await h.click('Остановить мониторинг'); expect(calls('/collection/stop')).toHaveLength(1); expect(h.app().text()).toContain('Мониторинг остановлен')
  })
  it('submits one-off collection options and does not invent collected counts', async () => {
    await h.launch('#status'); await h.field('Источник разового сбора', '1'); await h.click('Собрать сейчас')
    expect(last('/collection/runs').body).toEqual({ source_ids: [1], due_only: false, backfill: false, force: false })
    expect(h.app().text()).toContain('Разовый сбор запрошен'); expect(h.app().text()).not.toContain('Последний цикл:')
  })
  it('displays background failures and disables collection while busy', async () => {
    h.handle(call => call.url.pathname === '/api/v1/collection' ? json({ ...collection, busy: true, last_error: 'Ошибка фонового сбора' }) : call.url.pathname === '/api/v1/processing/runs' ? json({ runs: [{ ...processingRun, status: 'failed', error: 'Неверный ключ модели' }] }) : undefined)
    await h.launch('#status'); expect(h.button('Собрать сейчас').matches(':disabled')).toBe(true)
    expect(h.app().text()).toContain('Ошибка фонового сбора'); expect(h.app().text()).toContain('Неверный ключ модели')
  })
  it('polls running work, refreshes completed results, and stops polling on navigation', async () => {
    vi.useFakeTimers()
    let finished = false
    h.handle(call => call.url.pathname === '/api/v1/processing' ? json({ ...processing, running: finished ? null : processingRun, last: { ...processingRun, status: finished ? 'done' : 'running', finished_at: finished ? '2026-01-01T00:01:00Z' : null } }) : undefined)
    await h.launch('#status'); const initial = calls('/processing', 'GET').length
    finished = true; await vi.advanceTimersByTimeAsync(5000); await h.settle()
    expect(calls('/processing', 'GET').length).toBeGreaterThan(initial); expect(h.app().text()).toContain('Нет активного прогона')
    await h.app().get('a[href="#feed"]').trigger('click'); await h.settle(); const after = calls('/processing', 'GET').length
    await vi.advanceTimersByTimeAsync(15000); expect(calls('/processing', 'GET')).toHaveLength(after)
  })
})
