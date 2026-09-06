import { describe, expect, it, vi } from 'vitest'
import { harness, json } from './test/harness'
import { card, item, source } from './test/fixtures'
const h = harness()
const path = (value: string) => h.calls.filter(call => call.url.pathname === `/api/v1${value}`)
const mutation = (value: string, method = 'POST') => path(value).filter(call => call.method === method).slice(-1)[0]
const dialog = () => document.querySelector('dialog')!
async function openCard() { await h.click('Карточка и редактирование') }

describe('live dashboard and navigation', () => {
  it('keeps degraded readiness diagnostics and recovers after retry', async () => {
    h.handle(call => call.url.pathname === '/api/v1/health/ready' ? json({ status: 'degraded', app: 'hub', version: '2', environment: 'local', checks: { repository: 'unavailable' } }, 503) : undefined)
    await h.launch('#status')
    expect(h.app().text()).toContain('Сервер не готов'); expect(h.app().text()).toContain('ХранилищеНедоступно')
    expect(h.app().text()).not.toContain('Нет подключения')
    h.handle(() => undefined); await h.click('Повторить подключение')
    expect(h.app().text()).toContain('Сервер подключён'); expect(h.app().text()).not.toContain('Сервер не готов')
  })
  it('loads business records from the API and never from old demo storage', async () => {
    localStorage.setItem('analytics-hub:v1:news', JSON.stringify([{ title: 'OLD_DEMO_SENTINEL' }]))
    await h.launch()
    expect(h.app().text()).toContain('Материал сервера'); expect(h.app().text()).not.toContain('OLD_DEMO_SENTINEL')
    expect(path('/filters')).toHaveLength(1); expect(path('/status')).toHaveLength(1); expect(path('/health/ready')).toHaveLength(1)
    expect(h.app().text()).toContain('Сервер подключён')
  })
  it('renders an empty dataset without inventing news', async () => {
    h.handle(call => call.url.pathname === '/api/v1/items' ? json({ items: [], total: 0, next_cursor: null, took_ms: 1 }) : undefined)
    await h.launch(); expect(h.app().findAll('article')).toHaveLength(0); expect(h.app().text()).toContain('Материалов пока нет')
  })
  it('shows connection errors with a working retry', async () => {
    h.handle(() => json({ detail: 'Сервис недоступен' }, 503)); await h.launch()
    expect(h.app().text()).toContain('Сервис недоступен'); expect(h.app().findAll('article')).toHaveLength(0)
    h.handle(() => undefined); await h.click('Повторить подключение'); await h.click('Повторить')
    expect(h.app().text()).toContain('Материал сервера')
  })
  it('supports deep links and history navigation without retaining the wrong search', async () => {
    await h.launch('#npa'); expect(path('/items')[0]!.url.searchParams.get('type')).toBe('npa')
    await h.app().get('input[type="search"]').setValue('Поиск')
    history.replaceState(null, '', '#sources'); window.dispatchEvent(new HashChangeEvent('hashchange')); await h.settle()
    expect(h.app().get('h1').text()).toBe('Источники данных'); expect(h.app().get<HTMLInputElement>('input[type="search"]').element.value).toBe('')
    expect(path('/sources').length).toBeGreaterThan(0)
  })
  it('supports theme persistence, mobile navigation and the skip link', async () => {
    await h.launch()
    await h.app().get('[aria-label="Тёмная тема"]').trigger('click'); expect(document.documentElement.classList.contains('dark')).toBe(true)
    expect(localStorage.getItem('analytics-hub:theme')).toBe('dark')
    await h.app().get('[aria-label="Открыть навигацию"]').trigger('click'); expect(h.app().get('#sidebar').classes()).toContain('is-open')
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); await h.settle(); expect(h.app().get('#sidebar').classes()).not.toContain('is-open')
    await h.app().get('.skip-link').trigger('click'); expect(document.activeElement).toBe(h.app().get('main').element)
  })
})
describe('server-side feed filters and pagination', () => {
  it('sends source, category, priority, dates, sorting and full-text search to the API', async () => {
    await h.launch(); await h.field('Источник', '1'); await h.field('Категория / тег', 'тренды'); await h.field('Приоритет', 'high')
    await h.field('С даты', '2026-01-01'); await h.field('По дату', '2026-01-31'); await h.field('Порядок', 'priority')
    await h.app().get('input[type="search"]').setValue('ЦБ'); await h.settle()
    const query = path('/items').at(-1)!.url.searchParams
    expect(Object.fromEntries(query)).toMatchObject({ source_id: '1', tag: 'тренды', priority: 'high', from: '2026-01-01', to: '2026-01-31', order: 'priority', q: 'ЦБ' })
    expect(path('/items/facets').at(-1)!.url.search).toBe(path('/items').at(-1)!.url.search)
  })
  it('blocks reversed dates without requesting an invalid slice', async () => {
    await h.launch(); await h.field('С даты', '2026-02-01'); const count = path('/items').length
    await h.field('По дату', '2026-01-01'); expect(path('/items')).toHaveLength(count); expect(h.app().text()).toContain('Начало периода')
  })
  it('uses the opaque cursor for subsequent pages and resets it when filters change', async () => {
    h.handle(call => call.url.pathname === '/api/v1/items' ? json({ items: [{ ...item, id: call.url.searchParams.has('cursor') ? 102 : 101 }], total: 2, next_cursor: call.url.searchParams.has('cursor') ? null : 'opaque+/=', took_ms: 1 }) : undefined)
    await h.launch(); await h.click('Загрузить ещё'); expect(h.app().findAll('article')).toHaveLength(2)
    expect(path('/items').at(-1)!.url.searchParams.get('cursor')).toBe('opaque+/=')
    await h.field('Приоритет', 'low'); expect(path('/items').at(-1)!.url.searchParams.has('cursor')).toBe(false); expect(h.app().findAll('article')).toHaveLength(1)
  })
  it('submits selected IDs for bulk digest exclusion', async () => {
    await h.launch(); await h.app().get('[aria-label="Выбрать материал 101"]').setValue(true); await h.click('Исключить из дайджеста')
    expect(mutation('/items/bulk')!.body).toEqual({ item_ids: [101], scope: 'digest', reason: '' })
    expect(h.app().text()).toContain('Обновлено материалов: 1')
  })
  it('does not treat failed bulk updates as successful', async () => {
    h.handle(call => call.url.pathname === '/api/v1/items/bulk' ? json({ detail: 'Изменение отклонено' }, 500) : undefined)
    await h.launch(); await h.app().get('[aria-label="Выбрать материал 101"]').setValue(true); await h.click('Скрыть из ленты')
    expect(h.app().text()).toContain('Изменение отклонено'); expect(h.app().text()).not.toContain('Обновлено материалов')
  })
})
describe('material CRUD, notes, history and originals', () => {
  it('does not send a PATCH for unchanged material fields', async () => {
    await h.launch(); await openCard(); await h.click('Редактировать материал'); await h.submit()
    expect(mutation('/items/101', 'PATCH')).toBeUndefined(); expect(dialog().textContent).toContain('Нет изменений')
  })
  it('patches category tags without overriding untouched AI fields', async () => {
    await h.launch(); await openCard(); await h.click('Редактировать материал'); await h.field('Категория', 'репутация', dialog()); await h.submit()
    expect(mutation('/items/101', 'PATCH')!.body).toEqual({ tags: ['репутация', 'тренды', 'API'], edit_reason: 'other' })
  })
  it('edits the server record and preserves multiple category tags', async () => {
    await h.launch(); await openCard(); await h.click('Редактировать материал')
    await h.field('Заголовок', 'Правка аналитика', dialog()); await h.field('Саммари', 'Исправленное саммари', dialog()); await h.submit()
    expect(mutation('/items/101', 'PATCH')!.body).toEqual({ title: 'Правка аналитика', summary: 'Исправленное саммари', edit_reason: 'other' })
    expect(dialog().textContent).toContain('Изменения сохранены на сервере')
    expect(localStorage.getItem('analytics-hub:v1:news')).toBeNull()
  })
  it('keeps unsaved changes visible on PATCH failure and allows cancellation', async () => {
    h.handle(call => call.method === 'PATCH' ? json({ detail: 'Сохранение недоступно' }, 503) : undefined)
    await h.launch(); await openCard(); await h.click('Редактировать материал'); await h.field('Заголовок', 'Черновик', dialog()); await h.submit()
    expect(dialog().textContent).toContain('Сохранение недоступно'); expect(dialog().querySelector('input')!.value).toBe('Черновик')
    await h.click('Отмена'); expect(dialog().textContent).toContain('Материал сервера')
  })
  it('blocks blank titles and summaries before PATCH', async () => {
    await h.launch(); await openCard(); await h.click('Редактировать материал'); await h.field('Заголовок', '  ', dialog()); await h.submit()
    expect(mutation('/items/101', 'PATCH')).toBeUndefined(); expect(dialog().textContent).toContain('не могут быть пустыми')
  })
  it('adds a note and loads the revision endpoint', async () => {
    await h.launch(); await openCard(); await h.field('Новая заметка', 'Обсудить на совещании', dialog()); await h.field('Автор (необязательно)', 'Аналитик', dialog())
    await h.click('Добавить заметку'); expect(mutation('/items/101/notes')!.body).toEqual({ body: 'Обсудить на совещании', author: 'Аналитик' })
    await h.click('Обновить историю'); expect(path('/items/101/revisions')).toHaveLength(1)
    expect(dialog().textContent).toContain('Новые требования')
  })
  it('shows NPA events and restores the chosen model field', async () => {
    h.handle(call => call.url.pathname === '/api/v1/items/101' ? json({ ...card, item: { ...card.item, type: 'npa', npa_status: 'рассмотрение' }, events: [{ id: 1, status: 'анонс', created_at: '2026-01-01', note: 'Событие сервера' }], model_proposals: { summary: { id: 2, new_value: 'Предложение модели' } } }) : undefined)
    await h.launch(); await openCard(); expect(dialog().textContent).toContain('Событие сервера'); expect(h.button('Добавить событие / срок').disabled).toBe(false)
    await h.click('Вернуть версию модели: summary'); expect(mutation('/items/101/revert')!.body).toEqual({ field: 'summary' })
  })
  it('offers to merge a probable duplicate and to dismiss it', async () => {
    h.handle(call => call.url.pathname === '/api/v1/items/101' ? json({ ...card, duplicate_proposal: { items: [{ id: 102, title: 'Пересказ того же события', published_at: null }], similarity: 0.91, run_id: 3, created_at: '2026-01-01' } }) : undefined)
    await h.launch(); await openCard()
    expect(dialog().textContent).toContain('Вероятный дубль — объединить?'); expect(dialog().textContent).toContain('#102 «Пересказ того же события»'); expect(dialog().textContent).toContain('сходство 91%')
    await h.click('Объединить'); expect(mutation('/items/101/merge')!.body).toEqual({ item_ids: [102], reason: '' })
    await h.click('Не дубль'); expect(mutation('/items/101/not-duplicate')).toBeTruthy()
  })
  it('marks a card with an open duplicate proposal in the feed', async () => {
    h.handle(call => call.url.pathname === '/api/v1/items' ? json({ items: [{ ...item, flags: { ...item.flags, duplicate: true } }], total: 1, next_cursor: null, took_ms: 1 }) : undefined)
    await h.launch(); expect(h.app().text()).toContain('Вероятный дубль')
  })
  it('hides, unhides, deletes and restores through distinct backend actions', async () => {
    await h.launch(); await openCard(); await h.click('Скрыть из ленты'); expect(mutation('/items/101/hide')!.body).toMatchObject({ scope: 'feed' })
    await h.click('Вернуть в ленту'); expect(mutation('/items/101/unhide')).toBeDefined()
    await h.click('Удалить материал'); expect(mutation('/items/101', 'DELETE')).toBeDefined()
    await h.click('Восстановить материал'); expect(mutation('/items/101/restore')).toBeDefined()
  })
  it('does not render untrusted titles as HTML or allow unsafe original links', async () => {
    h.handle(call => call.url.pathname === '/api/v1/items' ? json({ items: [{ ...item, title: '<img src=x onerror=alert(1)>', canonical_url: 'javascript:alert(1)' }], total: 1, next_cursor: null, took_ms: 1 }) : undefined)
    await h.launch(); expect(h.app().get('article').text()).toContain('<img'); expect(h.app().find('article img').exists()).toBe(false); expect(h.app().find('article a').exists()).toBe(false)
  })
})
describe('manual materials', () => {
  it('does not offer duplicate override for unrelated conflict codes', async () => {
    h.handle(call => call.url.pathname === '/api/v1/items' && call.method === 'POST' ? json({ detail: 'Конфликт', code: 'other_conflict' }, 409) : undefined)
    await h.launch(); await h.click('+ Добавить материал'); await h.field('Заголовок', 'Материал', dialog()); await h.submit()
    expect(dialog().textContent).not.toContain('несмотря на возможный дубль')
  })
  it('creates an NPA with raw text, status and explicit AI choice', async () => {
    await h.launch('#npa'); await h.click('+ Добавить НПА'); await h.field('Заголовок', 'Ручной НПА', dialog()); await h.field('Текст материала', 'Полный текст', dialog())
    const checkbox = dialog().querySelector<HTMLInputElement>('input[type="checkbox"]')!; checkbox.checked = false; checkbox.dispatchEvent(new Event('change', { bubbles: true })); await h.submit()
    expect(mutation('/items')!.body).toMatchObject({ title: 'Ручной НПА', raw_text: 'Полный текст', type: 'npa', npa_status: 'анонс', run_llm: false, force: false })
  })
  it('does not create a duplicate automatically after a conflict', async () => {
    h.handle(call => call.url.pathname === '/api/v1/items' && call.method === 'POST' ? json({ detail: 'Возможный дубль', code: 'possible_duplicate', details: { item_id: 101 } }, 409) : undefined)
    await h.launch(); await h.click('+ Добавить материал'); await h.field('Заголовок', 'Материал', dialog()); await h.submit()
    expect(dialog().textContent).toContain('несмотря на возможный дубль'); expect(path('/items').filter(call => call.method === 'POST')).toHaveLength(1)
    expect(mutation('/items')!.body?.force).toBe(false)
  })
  it('explains where to find a manually submitted document awaiting processing', async () => {
    h.handle(call => call.url.pathname === '/api/v1/items' && call.method === 'POST' ? json({ id: null, document_id: 2, origin: 'manual', processing_status: 'queued' }, 201) : undefined)
    await h.launch(); await h.click('+ Добавить материал'); await h.field('Заголовок', 'Документ', dialog()); await h.submit()
    expect(document.querySelector('dialog')).toBeNull()
    expect(h.app().text()).toContain('Документ сохранён и ожидает обработки')
  })
})
describe('source APIs', () => {
  it('renames a source without rescheduling its next poll', async () => {
    await h.launch('#sources'); await h.click(source.name); await h.field('Название', 'Новое имя', dialog()); await h.submit()
    expect(mutation('/sources/1', 'PATCH')!.body).toEqual({ title: 'Новое имя' })
  })
  it('explicitly clears a source content hint with the backend empty-string convention', async () => {
    h.handle(call => call.url.pathname === '/api/v1/sources/1' && call.method === 'GET' ? json({ ...source, category_hint: 'npa' }) : undefined)
    await h.launch('#sources'); await h.click(source.name); await h.field('Содержание', '', dialog())
    // Vue represents the null-valued option as a DOM option with an internal value.
    const select = [...dialog().querySelectorAll('select')].find(node => node.options[0]?.text === 'Автоматически')!
    select.selectedIndex = 0; select.dispatchEvent(new Event('change', { bubbles: true })); await h.submit()
    expect(mutation('/sources/1', 'PATCH')!.body).toEqual({ category_hint: '' })
  })
  it('does not write unchanged source settings', async () => {
    await h.launch('#sources'); await h.click(source.name); await h.submit()
    expect(mutation('/sources/1', 'PATCH')).toBeUndefined()
  })
  it('shows poll counts and reloads server status even for a failed run', async () => {
    await h.launch('#sources'); await h.click('Опросить')
    expect(h.app().text()).toContain('Найдено: 1. Новых документов: 0.')
    const before = path('/status').length
    h.handle(call => call.url.pathname.endsWith('/refresh') ? json({ error_code: 'timeout', error_message: '' }) : undefined)
    await h.click('Опросить')
    expect(h.app().text()).toContain('timeout'); expect(h.app().text()).not.toContain('завершён')
    expect(path('/status').length).toBeGreaterThan(before)
  })
  it('creates a saved search from readable query fields', async () => {
    await h.launch('#sources'); await h.click('+ Добавить источник'); await h.field('Тип', 'search', dialog())
    await h.field('Поисковый запрос', 'закон об ИИ', dialog()); await h.field('Домены через запятую (необязательно)', 'example.org', dialog()); await h.submit()
    const params = new URL(String(mutation('/sources')!.body?.url))
    expect(params.protocol).toBe('tavily:'); expect(params.searchParams.get('q')).toBe('закон об ИИ'); expect(params.searchParams.get('domains')).toBe('example.org')
  })
  it('probes and creates a source using the backend field names and schedule', async () => {
    await h.launch('#sources'); await h.click('+ Добавить источник'); await h.field('URL источника', 'https://example.org/rss', dialog()); await h.click('Проверить источник')
    expect(mutation('/sources/probe')!.body).toEqual({ url: 'https://example.org/rss' }); await h.submit()
    expect(mutation('/sources')!.body).toMatchObject({ url: source.url, title: source.name, type: 'rss', poll_interval: '1h', backfill_limit: 20 })
  })
  it('blocks creation when the probe reports an existing source', async () => {
    h.handle(call => call.url.pathname === '/api/v1/sources/probe' ? json({ resolved_type: 'rss', title: 'Existing', suggested_poll_interval: '1h', already_exists: true, already_exists_source_id: 1, preview: [], warnings: [], note: '' }) : undefined)
    await h.launch('#sources'); await h.click('+ Добавить источник'); await h.field('URL источника', source.url, dialog()); await h.click('Проверить источник')
    expect(h.button('Сохранить источник').disabled).toBe(true); expect(mutation('/sources')).toBeUndefined()
  })
  it('edits the permitted source fields, pauses collection and inspects health', async () => {
    await h.launch('#sources'); await h.click(source.name); expect(path('/sources/1').length).toBeGreaterThan(0)
    await h.field('Название', 'Новое имя', dialog()); await h.field('Частота опроса', '15m', dialog()); await h.submit()
    expect(mutation('/sources/1', 'PATCH')!.body).toEqual({ title: 'Новое имя', poll_interval: '15m' })
    await h.click('Пауза'); expect(mutation('/sources/1', 'PATCH')!.body).toEqual({ status: 'paused' })
    await h.click('История опросов'); expect(path('/sources/1/health')[0]!.url.searchParams.get('limit')).toBe('20')
  })
  it('reports a collection error instead of announcing a successful refresh', async () => {
    h.handle(call => call.url.pathname.endsWith('/refresh') ? json({ error_code: 'timeout', error_message: 'Источник не ответил' }) : undefined)
    await h.launch('#sources'); await h.click('Опросить'); expect(h.app().text()).toContain('Источник не ответил'); expect(h.app().text()).not.toContain('завершён')
  })
  it('soft-deletes a source only after the user selects the deletion action', async () => {
    await h.launch('#sources'); await h.click('Удалить'); expect(mutation('/sources/1', 'DELETE')).toBeUndefined()
    await h.click('Удалить источник'); expect(mutation('/sources/1', 'DELETE')!.url.searchParams.get('purge_items')).toBe('false')
    expect(h.app().text()).toContain('Сохранено документов: 1')
  })
  it('requests deleted sources and restores their server IDs', async () => {
    h.handle(call => call.url.pathname === '/api/v1/sources' ? json({ sources: [{ ...source, status: 'deleted' }] }) : undefined)
    await h.launch('#sources'); await h.field('Статус источника', 'deleted'); expect(path('/sources').at(-1)!.url.searchParams.get('status')).toBe('deleted')
    await h.click('Восстановить'); expect(mutation('/sources/1/restore')).toBeDefined()
  })
})
describe('digest and processing capabilities', () => {
  it('clears an NPA-only status when switching the digest to news', async () => {
    await h.launch('#digest'); await h.field('Тип', 'npa'); await h.field('Статус НПА', 'действует'); await h.field('Тип', 'news'); await h.click('Сформировать дайджест')
    expect(mutation('/digest')!.body?.filters).toEqual({ type: 'news' })
  })
  it('requests the current slice, opt-in notes and format before exporting server content', async () => {
    await h.launch('#digest'); await h.field('Название дайджеста', 'Для руководителя'); await h.field('Формат', 'json')
    await h.click('Сформировать дайджест'); expect(mutation('/digest')!.body).toMatchObject({ format: 'json', title: 'Для руководителя', include_notes: false })
    expect(h.app().text()).toContain('{"items":[]}')
    const makeUrl = vi.fn((_blob: Blob) => 'blob:download'); const revoke = vi.fn()
    class TestURL extends URL { static createObjectURL = makeUrl; static revokeObjectURL = revoke }; vi.stubGlobal('URL', TestURL)
    vi.useFakeTimers(); const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    await h.click('Экспортировать'); expect(click).toHaveBeenCalledOnce(); expect(makeUrl.mock.calls[0]![0].type).toContain('application/json'); await vi.advanceTimersByTimeAsync(1000); expect(revoke).toHaveBeenCalledOnce()
  })
  it('prevents invalid JSON edits from being exported', async () => {
    await h.launch('#digest'); await h.field('Формат', 'json'); await h.click('Сформировать дайджест'); await h.click('Редактировать экспорт'); await h.app().get('textarea').setValue('{invalid')
    await h.click('Экспортировать'); expect(h.app().text()).toContain('Исправьте JSON')
  })
  it('loads the unprocessed queue with only its supported filters and marks missing controls', async () => {
    await h.launch('#status'); await h.field('Источник', '1'); const params = path('/documents').at(-1)!.url.searchParams
    expect(params.get('source_id')).toBe('1'); expect(params.has('priority')).toBe(false); expect(params.has('type')).toBe(false)
    expect(h.button('Обработать очередь ИИ').matches(':disabled')).toBe(false); expect(h.button('Запустить автоматический мониторинг').disabled).toBe(false)
    await h.click('Проверить процесс'); expect(path('/health')).toHaveLength(1)
  })
})
