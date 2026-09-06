// Real frontend client -> Vite proxy -> Python backend -> isolated SQLite.
// No production database, external sources, credentials or LLM calls are used.
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { createServer as createHttpServer } from 'node:http'
import { copyFile, mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const frontend = fileURLToPath(new URL('..', import.meta.url))
const repository = resolve(frontend, '..')
await mkdir(resolve(frontend, '.cache/tmp'), { recursive: true })
const root = await mkdtemp(resolve(frontend, '.cache/api-integration-'))
await copyFile(resolve(repository, 'config.yaml'), resolve(root, 'config.yaml'))
await writeFile(resolve(root, 'sources.json'), '[]\n')
const backend = spawn(process.env.INTEGRATION_PYTHON || resolve(repository, '.venv/bin/python'), [
  '-m', 'uvicorn', 'src.main:create_app', '--factory', '--host', '127.0.0.1', '--port', '0',
], { cwd: frontend, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1', PYTHONPATH: repository,
  HUB_ROOT: root, TMPDIR: resolve(frontend, '.cache/tmp'), OLLAMA_API_KEY: '', TAVILY_API: '', TELEGRAM_API_HASH: '',
  NO_PROXY: '127.0.0.1,localhost', no_proxy: '127.0.0.1,localhost',
}, stdio: ['ignore', 'pipe', 'pipe'] })
let logs = ''; let server; let feedServer
const originalFetch = globalThis.fetch
try {
  const target = await new Promise((resolveTarget, reject) => {
    const timer = setTimeout(() => reject(new Error(`Backend startup timed out:\n${logs}`)), 20000)
    const capture = chunk => {
      logs += chunk
      const match = logs.match(/Uvicorn running on (http:\/\/127\.0\.0\.1:\d+)/)
      if (match) { clearTimeout(timer); resolveTarget(match[1]) }
    }
    backend.stdout.on('data', capture); backend.stderr.on('data', capture)
    backend.once('error', error => { clearTimeout(timer); reject(error) })
    backend.once('exit', code => { clearTimeout(timer); reject(new Error(`Backend exited (${code}):\n${logs}`)) })
  })
  process.env.API_PROXY_TARGET = target
  server = await createServer({ root: frontend, server: { port: 0, strictPort: false }, logLevel: 'error' })
  await server.listen()
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`
  globalThis.fetch = (input, options) => originalFetch(new URL(input, origin), options)
  const { api, ApiError, request } = await server.ssrLoadModule('/src/api/client.ts')
  let checks = 0
  const check = (condition, message) => { assert.ok(condition, message); checks++ }
  async function until(read, ready) {
    for (let attempt = 0; attempt < 100; attempt++) {
      const result = await read()
      if (ready(result)) return result
      await new Promise(resolveWait => setTimeout(resolveWait, 100))
    }
    throw new Error('Background operation did not finish within 10 seconds')
  }
  check((await api.ready()).status === 'ok', 'readiness through frontend proxy')
  check((await api.health()).status === 'ok', 'health')
  check((await api.status()).items === 0, 'isolated database starts empty')
  check((await api.filters()).priorities.includes('high'), 'filter metadata')
  try { await request('/items', { query: { limit: 'not-a-number' } }); assert.fail('invalid query should fail') }
  catch (error) { check(error instanceof ApiError && error.status === 400 && error.code === 'validation_error', 'invalid query uses structured HTTP 400') }
  try { await request('/items', { method: 'POST', body: { title: 'Bad payload', unsupported: true } }); assert.fail('extra fields should fail') }
  catch (error) { check(error instanceof ApiError && error.status === 422 && error.code === 'validation_error', 'unknown request fields use structured HTTP 422') }
  check((await api.feed({})).total === 0, 'empty feed')
  check((await api.documents({})).total === 0, 'empty processing queue')

  const initialProfile = await api.activeProfile()
  check(initialProfile.id && (await api.profiles()).profiles.some(profile => profile.id === initialProfile.id), 'active profile is available in the profile list')
  const savedProfile = await api.saveProfile('Integration company', { industry: 'Software', products: ['Analytics'], custom: { retained: true } })
  check(savedProfile.version === 1 && (await api.profile(savedProfile.id)).payload.industry === 'Software', 'profile creation and detail lookup')
  const nextProfile = await api.saveProfile(savedProfile.name, { ...savedProfile.payload, topics: ['regulation'] })
  check(nextProfile.id === savedProfile.id && nextProfile.version === 2, 'profile edits increment version')
  await api.activateProfile(savedProfile.id)
  check((await api.activeProfile()).id === savedProfile.id, 'active profile switches on the server')

  const created = await api.createItem({ title: 'Проверка интеграции НПА', url: '', raw_text: 'Текст проверки подключения интерфейса к серверу.', type: 'npa', npa_status: 'анонс', run_llm: false, force: false })
  check(created.id && created.processing_status === 'done', 'manual material persisted')
  const id = created.id
  let card = await api.card(id)
  check(card.item.degraded && card.item.origin === 'manual', 'missing model is explicitly marked')
  check(card.sources.length === 1, 'original document linked')
  const event = await api.addEvent(id, { status: 'разработка', occurred_at: '2026-09-05T10:00:00Z', source_url: '', note: 'Первая стадия' })
  check(event.id && (await api.card(id)).item.npa_status === 'разработка', 'NPA event advances status')
  await api.editItem(id, { title: 'Новая редакция НПА', summary: 'Саммари аналитика.', priority: 'high', type: 'npa', npa_status: 'действует', tags: ['регуляторика', 'проверка'], edit_reason: 'wrong_focus' })
  const query = { q: 'редакция', type: 'npa', npa_status: 'действует', priority: ['high'], tag: ['регуляторика'], order: 'priority' }
  check((await api.feed(query)).items[0]?.id === id, 'combined feed filters and search')
  check((await api.facets(query)).total === 1, 'facets agree with feed')
  await api.note(id, 'Рассмотреть на совещании', 'Аналитик')
  card = await api.card(id)
  check(card.notes[0]?.body === 'Рассмотреть на совещании', 'notes saved')
  check((await api.revisions(id)).revisions.some(entry => entry.field === 'title'), 'revision history saved')
  check((await api.bulkTags([id], ['bulk-tag'], ['проверка'])).changed === 1, 'bulk tags reports changed cards')
  check((await api.card(id)).item.tags.includes('bulk-tag') && !(await api.card(id)).item.tags.includes('проверка'), 'bulk tags persists additions and removals')
  const csv = await api.exportFeed('csv', { type: 'npa', include_hidden: true, archived: 'include' })
  check(csv.includes('Новая редакция НПА') && !csv.includes('Рассмотреть на совещании'), 'CSV export uses actual cards and excludes analyst notes')
  const rss = await api.exportFeed('rss', { type: 'npa' })
  check(rss.includes('<rss') && rss.includes('Новая редакция НПА'), 'RSS export returns XML instead of JSON')
  await api.bulkArchive([id], true)
  check((await api.feed({})).total === 0 && !(await api.exportFeed('csv', {})).includes('Новая редакция НПА'), 'bulk archive removes cards from feed and export')
  await api.bulkArchive([id], false)
  check(!(await api.card(id)).item.is_archived, 'bulk unarchive persists')
  const digest = await api.digest({ type: 'npa' }, 'markdown', 'Обзор НПА', true)
  check(digest.items === 1 && digest.body.includes('Новая редакция НПА'), 'Markdown digest uses edited material')
  const json = await api.digest({}, 'json', '', true)
  check(json.items === 1 && typeof JSON.parse(json.body) === 'object', 'JSON digest')
  await api.addEvent(id, { status: 'Слушания', source_url: '', note: 'Срок рассмотрения' })
  check((await api.card(id)).events.some(event => event.status === 'Слушания'), 'custom event persists in chronology')
  await api.archive(id)
  check((await api.card(id)).item.is_archived, 'archive persisted')
  check((await api.feed({})).total === 0 && (await api.feed({ archived: 'only' })).total === 1, 'archive filter')
  check((await api.digest({ archived: 'include' }, 'markdown', '', false)).items === 0, 'digest always excludes archive')
  await api.unarchive(id)
  check(!(await api.card(id)).item.is_archived && (await api.feed({})).total === 1, 'unarchive persisted')
  await api.hideItem(id, 'digest', 'Адресный обзор')
  check((await api.digest({}, 'markdown', '', false)).items === 0, 'hidden item excluded from digest')
  await api.unhideItem(id)
  check((await api.bulk([id], 'feed', 'Скрыть')).changed === 1, 'bulk hide')
  check((await api.feed({})).total === 0, 'hidden item excluded from default feed')
  check((await api.feed({ include_hidden: true })).total === 1, 'hidden item remains recoverable')
  await api.bulk([id], 'visible', '')
  await api.deleteItem(id)
  check((await api.card(id)).item.visibility === 'deleted', 'soft deletion')
  await api.restoreItem(id)
  check((await api.feed({})).total === 1, 'restoration')
  const sources = (await api.sources()).sources
  check(sources.length === 1 && sources[0].kind === 'manual', 'manual source provisioned by backend')
  const sourceId = sources[0].id
  check((await api.source(sourceId)).id === sourceId, 'source details')
  await api.editSource(sourceId, { title: 'Ручные материалы', poll_interval: '24h', status: 'paused' })
  check((await api.sources({ status: 'paused', kind: 'manual' })).sources[0]?.name === 'Ручные материалы', 'source editing and filters')
  check((await api.sourceHealth(sourceId)).documents === 1, 'source health counts actual documents')
  const deleted = await api.deleteSource(sourceId, false)
  check(deleted.documents_kept === 1 && deleted.tracked_npa === 1, 'source deletion retains documents and reports tracked NPA')
  check((await api.sources({ status: 'deleted' })).sources.length === 1, 'deleted source remains recoverable')
  await api.restoreSource(sourceId)
  check((await api.source(sourceId)).status === 'active', 'source restored')
  // Exercise real RSS detection and collection using a loopback-only test feed.
  let feedUnavailable = false; let secondArticle = false
  feedServer = createHttpServer((request, response) => {
    if (feedUnavailable) { response.writeHead(503); response.end('Feed temporarily unavailable'); return }
    response.setHeader('Content-Type', 'application/rss+xml')
    const extra = secondArticle ? `<item><title>Second queue document</title><link>http://127.0.0.1:${feedServer.address().port}/second</link><guid>integration-rss-2</guid><pubDate>${new Date().toUTCString()}</pubDate><description>Independent second document for pagination.</description></item>` : ''
    response.end(`<?xml version="1.0"?><rss version="2.0"><channel><title>Integration RSS</title><link>http://127.0.0.1/</link><description>Test-only source</description><item><title>RSS integration document</title><link>http://127.0.0.1:${feedServer.address().port}/article</link><guid>integration-rss-1</guid><pubDate>${new Date().toUTCString()}</pubDate><description>${'Local integration test article. '.repeat(30)}</description></item>${extra}</channel></rss>`)
  })
  feedServer.listen(0, '127.0.0.1'); await once(feedServer, 'listening')
  const feedUrl = `http://127.0.0.1:${feedServer.address().port}/rss.xml`
  const probe = await api.probe(feedUrl)
  check(probe.resolved_type === 'rss' && probe.preview.length === 1, `real source detection and preview: ${JSON.stringify(probe)}`)
  const added = await api.createSource({ url: feedUrl, title: 'Integration RSS', type: 'rss', poll_interval: '1h', category_hint: 'news', backfill_limit: 0, created_by: '' })
  check(added.kind === 'rss', 'source creation')
  const renamed = await api.editSource(added.id, { title: 'Renamed RSS' })
  check(renamed.next_run_at === added.next_run_at, 'name-only edit preserves collection schedule')
  await api.editSource(added.id, { category_hint: '' })
  check((await api.source(added.id)).category_hint === null, 'empty content hint restores automatic classification')
  check((await api.probe(feedUrl)).already_exists, 'duplicate source detected')
  const collected = await api.refreshSource(added.id)
  check(!collected.error_code && collected.items_new === 1, 'source refresh collects a real document')
  check((await api.documents({ source_id: [added.id] })).total === 1, 'collected document visible in processing queue')
  check((await api.sourceHealth(added.id)).runs.length === 1, 'collection history recorded')
  feedUnavailable = true
  const failedRun = await api.refreshSource(added.id)
  check(!!failedRun.error_code, 'failed collection is returned as a run rather than thrown as an HTTP error')
  check((await api.sourceHealth(added.id)).consecutive_failures === 1, 'failed poll updates health history')
  feedUnavailable = false; secondArticle = true
  const moved = await api.editSource(added.id, { url: feedUrl.replace('/rss.xml', '/moved.xml'), type: 'rss', fetch_url: feedUrl.replace('/rss.xml', '/moved.xml') })
  check(moved.id === added.id && moved.url.endsWith('/moved.xml'), 'source relocated without changing its ID')
  check((await api.sourceHealth(added.id)).documents === 1, 'relocation preserves original documents')
  const previousCycle = (await api.collection()).last_collect?.id
  await api.collect({ source_ids: [added.id], due_only: false, backfill: false, force: false })
  const cycle = await until(() => api.collection(), state => !state.busy && state.last_collect?.id !== previousCycle)
  check(cycle.last_collect.docs_new === 1, 'background one-off collection adds the second document')
  const firstPage = await api.documents({ source_id: [added.id], limit: 1 })
  check(firstPage.total === 2 && firstPage.next_cursor, 'document cursor returned')
  const secondPage = await api.documents({ source_id: [added.id], limit: 1, cursor: firstPage.next_cursor })
  check(secondPage.documents.length === 1 && secondPage.documents[0].id !== firstPage.documents[0].id && !secondPage.next_cursor, 'next document page has no duplicate')
  const fetchedPage = await api.documents({ source_id: [added.id], order: 'fetched', limit: 1 })
  check(fetchedPage.documents[0].fetched_at && fetchedPage.documents[0].last_error === '', 'queue exposes collection date and failure detail')
  const fetchedNext = await api.documents({ source_id: [added.id], order: 'fetched', limit: 1, cursor: fetchedPage.next_cursor })
  check(fetchedNext.documents[0].id !== fetchedPage.documents[0].id, 'collection-time sorting paginates without duplicates')
  check(!(await api.processing()).llm_available, 'isolated processing explicitly has no LLM')
  const run = await api.startProcessing({ source_id: added.id, limit: 2, force: false })
  check(run.id && run.status === 'running', 'processing accepted as a background run')
  const completed = await until(() => api.processingRun(run.id), result => result.status !== 'running')
  check(completed.status === 'done' && completed.documents === 2 && completed.degraded > 0, 'processing completes with honest degraded results')
  check(completed.processed === 2 && completed.progress === 1 && completed.heartbeat_at, 'document-based progress and heartbeat are returned')
  const retried = await api.startProcessing({ only_failed: true, force: false, profile_id: savedProfile.id, limit: 2 })
  const retriedDone = await until(() => api.processingRun(retried.id), result => result.status !== 'running')
  check(retriedDone.params.only_failed && retriedDone.params.profile_id === savedProfile.id && retriedDone.documents === 0, 'failed-only retry uses selected profile and skips successful documents')
  const quality = await api.quality()
  check(quality.items > 0 && quality.degraded > 0 && quality.queue.failed === 0 && quality.calls === 0, 'quality reflects degraded cards without inventing model calls')
  check(Array.isArray((await api.quality({ since: '2026-01-01T00:00:00Z', until: '2027-01-01T00:00:00Z' })).by_day), 'quality accepts a bounded reporting window')
  check((await api.processingRuns()).runs.some(entry => entry.id === run.id), 'processing history includes completed run')
  check((await api.documents({ source_id: [added.id] })).total === 0, 'processed documents leave the queue')
  const started = await api.startCollection(900)
  check(started.running, 'automatic monitoring starts')
  await until(() => api.collection(), result => !result.busy)
  check(!(await api.stopCollection()).running, 'automatic monitoring stops')
  try { await api.card(999999); assert.fail('missing card should fail') }
  catch (error) { check(error instanceof ApiError && error.status === 404, 'real HTTP problem translated by frontend') }
  console.log(`PASS: ${checks} live integration checks (frontend client → Vite proxy → backend).`)
} catch (error) {
  console.error(logs)
  throw error
} finally {
  globalThis.fetch = originalFetch
  await server?.close()
  if (feedServer) await new Promise(resolveClose => feedServer.close(resolveClose))
  if (backend.exitCode === null && !backend.killed) {
    const stopped = once(backend, 'exit')
    backend.kill('SIGTERM')
    await stopped
  }
  await rm(root, { recursive: true, force: true })
}
