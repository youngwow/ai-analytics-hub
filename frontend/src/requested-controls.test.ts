import { describe, expect, it } from 'vitest'
import { harness, json } from './test/harness'
import { collection, processing, processingRun } from './test/fixtures'
const h = harness()
const calls = (path: string, method = 'POST') => h.calls.filter(call => call.url.pathname === `/api/v1${path}` && call.method === method)
async function checkbox(label: string, value: boolean) { await h.app().findAll('label').find(node => node.text() === label)!.get('input').setValue(value) }

describe('digest Markdown preview', () => {
  it('renders headings, lists, links and tables, and reflects edits without changing Markdown export', async () => {
    h.handle(call => call.url.pathname === '/api/v1/digest' ? json({ title: 'Дайджест', format: 'markdown', generated_at: '2026-09-07', items: 1, body: '# Заголовок\n\n- **Важно**\n\n[Оригинал](https://example.org)\n\n| День | Итог |\n| --- | --- |\n| Пн | Готово |' }) : undefined)
    await h.launch('#digest'); await h.click('Сформировать дайджест')
    const preview = h.app().get('[aria-label="Предпросмотр дайджеста"]')
    expect(preview.get('h1').text()).toBe('Заголовок'); expect(preview.get('li strong').text()).toBe('Важно'); expect(preview.get('a').attributes('href')).toBe('https://example.org')
    expect(preview.get('table').text()).toContain('Готово')
    await h.click('Редактировать экспорт'); await h.app().get('textarea[aria-label="Текст дайджеста"]').setValue('## Новый заголовок\n\nТекст')
    await h.click('Завершить правку'); expect(h.app().get('[aria-label="Предпросмотр дайджеста"] h2').text()).toBe('Новый заголовок')
  })
  it('does not execute raw HTML or render script links from document text', async () => {
    h.handle(call => call.url.pathname === '/api/v1/digest' ? json({ title: 'Дайджест', format: 'markdown', generated_at: '2026-09-07', items: 1, body: '<img src=x onerror="alert(1)">\n<script>alert(1)</script>\n\n[Unsafe](javascript:alert(1))' }) : undefined)
    await h.launch('#digest'); await h.click('Сформировать дайджест')
    const preview = h.app().get('[aria-label="Предпросмотр дайджеста"]')
    expect(preview.find('script').exists()).toBe(false); expect(preview.find('[onerror]').exists()).toBe(false); expect(preview.find('a[href^="javascript:"]').exists()).toBe(false)
  })
  it('keeps JSON exports as literal text', async () => {
    await h.launch('#digest'); await h.field('Формат', 'json'); await h.click('Сформировать дайджест')
    expect(h.app().find('[aria-label="Предпросмотр дайджеста"]').exists()).toBe(false)
    expect(h.app().get('pre').text()).toBe('{"items":[]}')
  })
})

describe('queue and collection controls', () => {
  it('defaults to unlimited processing and sends an explicit cap only when enabled', async () => {
    await h.launch('#status'); expect(h.app().text()).toContain('Без ограничения')
    await h.click('Обработать очередь ИИ'); expect(calls('/processing/runs')[0]!.body?.limit).toBeNull()
    await checkbox('Ограничить количество документов', true); await h.field('Лимит обработки', '48'); await h.click('Обработать очередь ИИ')
    expect(calls('/processing/runs')[1]!.body?.limit).toBe(48)
  })
  it('requests a stop and keeps processing disabled until the server confirms completion', async () => {
    let stopping = false
    h.handle(call => {
      if (call.url.pathname === '/api/v1/processing/runs/7/stop') { stopping = true; return json({ ...processingRun, stop_requested: true }) }
      if (call.url.pathname === '/api/v1/processing') return json({ ...processing, running: { ...processingRun, stop_requested: stopping } })
      return undefined
    })
    await h.launch('#status'); await h.click('Остановить обработку ИИ')
    expect(calls('/processing/runs/7/stop')).toHaveLength(1)
    expect(h.button('Остановка запрошена').disabled).toBe(true); expect(h.button('Обработать очередь ИИ').matches(':disabled')).toBe(true)
    expect(h.app().text()).toContain('Ожидаем завершения текущих документов')
  })
  it('reports stop failures and permits retry', async () => {
    h.handle(call => call.url.pathname === '/api/v1/processing' ? json({ ...processing, running: processingRun }) : call.url.pathname.endsWith('/stop') ? json({ detail: 'Остановка недоступна' }, 503) : undefined)
    await h.launch('#status'); await h.click('Остановить обработку ИИ')
    expect(h.app().text()).toContain('Остановка недоступна'); expect(h.button('Остановить обработку ИИ').disabled).toBe(false)
  })
  it('displays a stopped run as stopped and enables a new run', async () => {
    h.handle(call => call.url.pathname === '/api/v1/processing' ? json({ ...processing, last: { ...processingRun, status: 'failed', stopped: true, stop_requested: true, finished_at: '2026-09-07', error: 'Остановлен пользователем' } }) : undefined)
    await h.launch('#status'); expect(h.app().get('[aria-label="Текущая обработка"]').text()).toContain('Остановлен')
    expect(h.button('Обработать очередь ИИ').matches(':disabled')).toBe(false)
  })
  it('converts monitoring minutes to seconds and submits the selected age window', async () => {
    await h.launch('#status'); await h.field('Интервал мониторинга, минут', '5'); await h.field('Окно автоматического сбора, часов', '168'); await h.click('Запустить автоматический мониторинг')
    expect(calls('/collection/start')[0]!.body).toEqual({ interval_seconds: 300, date_window_hours: 168 })
  })
  it('loads the active monitoring interval and age window from the server', async () => {
    h.handle(call => call.url.pathname === '/api/v1/collection' ? json({ ...collection, running: true, interval_seconds: 120, date_window_hours: 24 }) : undefined)
    await h.launch('#status')
    const input = h.app().findAll('label').find(node => node.text() === 'Интервал мониторинга, минут')!.get('input')
    expect(input.element.value).toBe('2'); expect(h.app().text()).toContain('последние 24 ч.')
  })
  it('uses the one-off age window and explicitly bypasses it for backfill', async () => {
    await h.launch('#status'); await h.field('Окно разового сбора, часов', '12'); await h.click('Собрать сейчас')
    expect(calls('/collection/runs')[0]!.body?.date_window_hours).toBe(12)
    await checkbox('Собрать предыдущие публикации без ограничения по дате', true); await h.click('Собрать сейчас')
    expect(calls('/collection/runs')[1]!.body).toMatchObject({ backfill: true }); expect(calls('/collection/runs')[1]!.body).not.toHaveProperty('date_window_hours')
  })
  it('sends card numbers unchanged to server-side search', async () => {
    await h.launch(); await h.app().get('input[aria-label="Поиск в текущем разделе"]').setValue('#48')
    await new Promise(resolve => setTimeout(resolve, 0)); await h.settle()
    expect(calls('/items', 'GET').slice(-1)[0]!.url.searchParams.get('q')).toBe('#48')
  })
})
