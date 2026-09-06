import { describe, expect, it, vi } from 'vitest'
import { harness, json } from './test/harness'
import { processing, processingRun, profile, quality } from './test/fixtures'
const h = harness()
const calls = (path: string, method = 'POST') => h.calls.filter(call => call.url.pathname === `/api/v1${path}` && call.method === method)
const last = (path: string, method = 'POST') => calls(path, method).slice(-1)[0]!
async function selectItem() { await h.app().get('input[aria-label="Выбрать материал 101"]').setValue(true) }

describe('company profiles', () => {
  it('loads the active profile and edits a new version without losing extra payload fields', async () => {
    await h.launch('#profiles'); expect(calls('/profiles/active', 'GET')).toHaveLength(1)
    await h.click(profile.name); expect(calls('/profiles/1', 'GET')).toHaveLength(1)
    await h.field('Продукты', 'Первый\nВторой\nПервый'); await h.click('Сохранить профиль')
    expect(last('/profiles').body).toEqual({ name: profile.name, payload: { ...profile.payload, products: ['Первый', 'Второй'] } })
    expect(h.app().text()).toContain('Профиль сохранён · версия 3')
  })
  it('creates a profile, validates empty content, and retains a rejected draft', async () => {
    await h.launch('#profiles'); await h.click('Создать профиль'); await h.field('Название профиля', 'Новая компания')
    await h.click('Сохранить профиль'); expect(calls('/profiles')).toHaveLength(0)
    expect(h.app().text()).toContain('Заполните хотя бы одно поле')
    h.handle(call => call.method === 'POST' && call.url.pathname === '/api/v1/profiles' ? json({ detail: 'Профиль отклонён' }, 422) : undefined)
    await h.field('Отрасль', 'Разработка ПО'); await h.click('Сохранить профиль')
    expect(last('/profiles').body).toEqual({ name: 'Новая компания', payload: { industry: 'Разработка ПО' } })
    expect(h.app().text()).toContain('Профиль отклонён'); expect(h.app().get('textarea').element.value).toBe('Разработка ПО')
  })
  it('changes the active profile using the default endpoint', async () => {
    h.handle(call => call.url.pathname === '/api/v1/profiles' ? json({ profiles: [profile, { ...profile, id: 2, name: 'Второй профиль', is_default: false }] }) : undefined)
    await h.launch('#profiles'); await h.click('Использовать Второй профиль')
    expect(calls('/profiles/2/default')).toHaveLength(1)
  })
  it('prevents accidental overwrite when a new profile name already exists', async () => {
    await h.launch('#profiles'); await h.click('Создать профиль'); await h.field('Название профиля', profile.name); await h.field('Отрасль', 'Новая отрасль'); await h.click('Сохранить профиль')
    expect(calls('/profiles')).toHaveLength(0); expect(h.app().text()).toContain('Откройте его для редактирования')
  })
})

describe('processing telemetry and retries', () => {
  it('sends only_failed and the selected profile with the normal processing request', async () => {
    await h.launch('#status'); await h.field('Профиль обработки', '1')
    // Locate by label so adding collection options cannot change this action.
    const label = h.app().findAll('label').find(label => label.text() === 'Только сбойные документы')!
    await label.get('input').setValue(true); await h.click('Обработать очередь ИИ')
    expect(last('/processing/runs').body).toMatchObject({ only_failed: true, profile_id: 1, limit: 10 })
  })
  it('shows document-based progress and heartbeat without mistaking clusters for documents', async () => {
    h.handle(call => call.url.pathname === '/api/v1/processing' ? json({ ...processing, failed: 3, running: { ...processingRun, documents: 10, processed: 6, clusters: 2, progress: 0.6, heartbeat_at: '2026-01-01T00:01:00Z' } }) : undefined)
    await h.launch('#status')
    expect(h.app().text()).toContain('Обработано документов: 6 / 10'); expect(h.app().text()).toContain('60%')
    expect(h.app().get('progress').attributes('value')).toBe('0.6'); expect(h.app().text()).toContain('Последний сигнал сервера')
    expect(h.app().text()).toContain('Сбойных документов: 3')
  })
  it('keeps processing usable when optional profiles and quality fail', async () => {
    h.handle(call => ['/api/v1/profiles', '/api/v1/processing/quality'].includes(call.url.pathname) ? json({ detail: 'Отчёт недоступен' }, 503) : undefined)
    await h.launch('#status'); expect(h.button('Обработать очередь ИИ').matches(':disabled')).toBe(false)
    await h.click('Обработать очередь ИИ'); expect(last('/processing/runs').body).not.toHaveProperty('profile_id')
  })
  it('loads real quality breakdowns and validates the requested time window', async () => {
    await h.launch('#status'); const panel = h.app().get('[aria-label="Качество модели"]')
    expect(panel.text()).toContain('Среднее время вызова: 12,5 сек.'); expect(panel.text()).toContain('30%')
    expect(panel.get('[aria-label="Вызовы модели по дням"]').text()).toContain(quality.by_day[0]!.day)
    await h.field('Вызовы с даты', '2026-09-02T00:00'); await h.field('Вызовы до даты', '2026-09-01T00:00')
    expect(h.button('Обновить качество').disabled).toBe(true)
    await h.field('Вызовы до даты', '2026-09-03T00:00'); await h.click('Обновить качество')
    expect(last('/processing/quality', 'GET').url.searchParams.get('since')).toBe(new Date('2026-09-02T00:00').toISOString())
  })
  it('sorts the document queue by collection time and displays document errors', async () => {
    h.handle(call => call.url.pathname === '/api/v1/documents' ? json({ documents: [{ id: 1, title: 'Документ', url: '', source_id: 1, source_name: 'Источник', published_at: null, fetched_at: '2026-09-01T00:00:00Z', last_error: 'Ошибка модели', chars: 10 }], total: 1, next_cursor: null, took_ms: 1 }) : undefined)
    await h.launch('#status'); await h.field('Порядок', 'fetched')
    expect(last('/documents', 'GET').url.searchParams.get('order')).toBe('fetched')
    expect(h.app().get('[aria-label="Необработанные документы"]').text()).toContain('Ошибка модели')
  })
})

describe('bulk editing and exports', () => {
  it.each([['Архивировать выбранные', true], ['Вернуть выбранные из архива', false]] as const)('%s sends selected IDs and archive state', async (label, archived) => {
    await h.launch(); await selectItem(); await h.click(label)
    expect(last('/items/archive/bulk').body).toEqual({ item_ids: [101], archived })
    expect(h.app().text()).toContain('Обновлено материалов: 1')
  })
  it('adds and removes tags without resending the rest of the card', async () => {
    await h.launch(); await selectItem(); await h.field('Добавить теги', 'проверка, тренды, проверка'); await h.field('Снять теги', 'API'); await h.click('Применить теги')
    expect(last('/items/tags/bulk').body).toEqual({ item_ids: [101], add: ['проверка', 'тренды'], remove: ['API'] })
  })
  it('keeps selection and tags after a failed bulk update and rejects contradictory tags', async () => {
    await h.launch(); await selectItem(); await h.field('Добавить теги', 'API'); await h.field('Снять теги', 'API'); await h.click('Применить теги')
    expect(calls('/items/tags/bulk')).toHaveLength(0)
    h.handle(call => call.url.pathname.endsWith('/tags/bulk') ? json({ detail: 'Теги не сохранены' }, 500) : undefined)
    await h.field('Снять теги', ''); await h.click('Применить теги')
    expect(h.app().text()).toContain('Теги не сохранены'); expect(h.app().text()).toContain('Выбрано: 1')
    expect(h.app().text()).not.toContain('Обновлено материалов')
  })
  it.each(['csv', 'rss'] as const)('downloads %s with current filters, excluding hidden/archive and pagination', async format => {
    const create = vi.fn(() => 'blob:export')
    class TestURL extends URL { static createObjectURL = create; static revokeObjectURL = vi.fn() }
    vi.stubGlobal('URL', TestURL); vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    h.handle(call => call.url.pathname.startsWith('/api/v1/export/') ? new Response(format === 'csv' ? 'id,title\n101,Материал' : '<?xml version="1.0"?><rss/>', { headers: { 'Content-Type': format === 'csv' ? 'text/csv' : 'application/rss+xml' } }) : undefined)
    await h.launch(); await h.field('Приоритет', 'high'); await h.field('Архив', 'include'); await h.click(format === 'csv' ? 'Скачать CSV' : 'Скачать RSS')
    const query = last(format === 'csv' ? '/export/items.csv' : '/export/feed.xml', 'GET').url.searchParams
    expect(query.get('priority')).toBe('high'); expect(query.get('limit')).toBe('200'); expect(query.get('archived')).toBe('exclude'); expect(query.get('include_hidden')).toBe('false'); expect(query.has('cursor')).toBe(false)
    expect(create).toHaveBeenCalledOnce(); expect(h.app().text()).toContain('Экспорт готов')
  })
  it('reports export problems without offering a success download', async () => {
    h.handle(call => call.url.pathname.startsWith('/api/v1/export/') ? json({ detail: 'Экспорт недоступен' }, 503) : undefined)
    await h.launch(); await h.click('Скачать CSV'); expect(h.app().text()).toContain('Экспорт недоступен'); expect(h.app().text()).not.toContain('Экспорт готов')
  })
})
