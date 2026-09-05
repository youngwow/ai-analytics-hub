<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api } from '../api/client'
import type { Probe, Source, SourceCreate, SourceHealth, SourceStatus, SourceUpdate } from '../api/types'
import { useDashboard } from '../composables/dashboard'
import { useRemote } from '../composables/remote'
import { INTERVALS, SOURCE_STATUSES, SOURCE_TYPES } from '../data/dashboard'
import { formatDate, normalizeSourceUrl, safeUrl } from '../utils/dashboard'
import AppModal from './AppModal.vue'
const store = useDashboard(); const { search, revision } = store
const kind = ref(''); const statusFilter = ref('')
const { data: sources, loading, error, run } = useRemote<Source[]>([])
const filtered = computed(() => sources.value.filter(source => `${source.name} ${source.url}`.toLocaleLowerCase('ru').includes(search.value.trim().toLocaleLowerCase('ru'))))
function load() { return run(async signal => (await api.sources({ kind: kind.value || undefined, status: statusFilter.value || undefined }, signal)).sources) }
watch([kind, statusFilter, revision], () => { void load() }, { immediate: true })
const modal = ref(false); const editing = ref<Source | null>(null); const busy = ref(false); const actionError = ref(''); const message = ref('')
const probe = ref<Probe | null>(null); const health = ref<SourceHealth | null>(null); const healthId = ref<number | null>(null); const healthLoading = ref(false); const healthError = ref('')
const deleting = ref<Source | null>(null); const purge = ref(false)
const searchQuery = ref(''); const searchDomains = ref(''); const searchDays = ref(7)
const sourceUrl = () => draft.value.type === 'search' && !editing.value
  ? `tavily://search?${new URLSearchParams({ q: searchQuery.value.trim(), domains: searchDomains.value.trim(), days: String(searchDays.value), topic: 'news', summary: '1' })}`
  : normalizeSourceUrl(draft.value.url)
const draft = ref<SourceCreate>({ url: '', title: '', type: '', poll_interval: '1h', category_hint: null, backfill_limit: 20, created_by: '' })
watch([() => draft.value.url, () => draft.value.type, searchQuery, searchDomains, searchDays], () => { probe.value = null }, { flush: 'sync' })
function openNew() { editing.value = null; searchQuery.value = ''; searchDomains.value = ''; searchDays.value = 7; draft.value = { url: '', title: '', type: '', poll_interval: '1h', category_hint: null, backfill_limit: 20, created_by: '' }; probe.value = null; actionError.value = ''; modal.value = true }
async function openEdit(id: number) {
  actionError.value = ''; busy.value = true
  try { editing.value = await api.source(id); const source = editing.value; draft.value = { url: source.url, title: source.name, type: source.kind, poll_interval: source.poll_interval, category_hint: source.category_hint, backfill_limit: 0, created_by: source.created_by }; modal.value = true }
  catch (reason) { actionError.value = (reason as Error).message } finally { busy.value = false }
}
async function detect() {
  if (busy.value) return
  busy.value = true; actionError.value = ''; probe.value = null
  try { const result = await api.probe(sourceUrl()); if (!draft.value.title) draft.value.title = result.title; if (result.resolved_type !== 'unsupported') draft.value.type = result.resolved_type; if (result.suggested_poll_interval) draft.value.poll_interval = result.suggested_poll_interval; probe.value = result }
  catch (reason) { actionError.value = (reason as Error).message } finally { busy.value = false }
}
async function save() {
  if (busy.value) return
  busy.value = true; actionError.value = ''
  try {
    if (editing.value) {
      if (!draft.value.title.trim()) throw new Error('Укажите название источника.')
      const changes: SourceUpdate = {}
      if (draft.value.title.trim() !== editing.value.name) changes.title = draft.value.title.trim()
      if (draft.value.poll_interval !== editing.value.poll_interval) changes.poll_interval = draft.value.poll_interval
      if ((draft.value.category_hint || null) !== (editing.value.category_hint || null)) changes.category_hint = draft.value.category_hint || ''
      if (!Object.keys(changes).length) { modal.value = false; message.value = 'Нет изменений для сохранения.'; return }
      await api.editSource(editing.value.id, changes)
    }
    else await api.createSource({ ...draft.value, url: sourceUrl(), title: draft.value.title.trim() })
    modal.value = false; message.value = editing.value ? 'Источник обновлён.' : 'Источник добавлен. Первичный сбор запущен, если включён.'; store.changed()
  } catch (reason) { actionError.value = (reason as Error).message } finally { busy.value = false }
}
async function action(task: () => Promise<unknown>, success: string | (() => string)) {
  if (busy.value) return
  busy.value = true; actionError.value = ''; message.value = ''
  try { await task(); message.value = typeof success === 'function' ? success() : success; store.changed() } catch (reason) { actionError.value = (reason as Error).message } finally { busy.value = false }
}
async function refresh(source: Source) {
  if (busy.value) return
  busy.value = true; actionError.value = ''; message.value = ''
  try {
    const result = await api.refreshSource(source.id)
    if (result.error_code || result.error_message) throw new Error(result.error_message || result.error_code)
    message.value = `Опрос «${source.name}» завершён. Найдено: ${result.items_found}. Новых документов: ${result.items_new}.`
  } catch (reason) { actionError.value = (reason as Error).message }
  finally { busy.value = false; store.changed() }
}
async function inspect(id: number) {
  healthId.value = id; health.value = null; healthError.value = ''; healthLoading.value = true
  try { const result = await api.sourceHealth(id); if (healthId.value === id) health.value = result } catch (reason) { healthError.value = (reason as Error).message } finally { healthLoading.value = false }
}
async function remove() {
  if (!deleting.value || busy.value) return
  const source = deleting.value
  let details = ''
  await action(async () => {
    const result = await api.deleteSource(source.id, purge.value)
    deleting.value = null
    details = `Сохранено документов: ${result.documents_kept}. Скрыто материалов: ${result.items_hidden}. НПА на контроле: ${result.tracked_npa}.`
  }, () => `Источник «${source.name}» удалён. ${details}`)
}
</script>
<template>
  <div class="view-column">
    <div class="toolbar gap-4 py-3"><span class="text-muted">Источников: {{ sources.length }}</span><button class="outline-button ml-auto" :disabled="busy" @click="openNew">+ Добавить источник</button><button class="text-link" :disabled="loading" @click="load">Обновить список</button></div>
    <div class="toolbar gap-3 py-2"><label class="field-label">Тип источника<select v-model="kind" class="form-control"><option value="">Все типы</option><option v-for="(label, key) in SOURCE_TYPES" :key="key" :value="key">{{ label }}</option></select></label><label class="field-label">Статус источника<select v-model="statusFilter" class="form-control"><option value="">Все, кроме удалённых</option><option v-for="(label, key) in SOURCE_STATUSES" :key="key" :value="key">{{ label }}</option></select></label></div>
    <p v-if="error || actionError" role="alert" class="feedback-bar">{{ error || actionError }}<button class="text-link" @click="load">Повторить</button></p><p v-if="message" role="status" class="feedback-bar">{{ message }}</p>
    <div class="table-scroll" :aria-busy="loading"><p v-if="loading" class="p-4 text-muted" role="status">Загрузка источников…</p><table class="data-table sources-table" aria-label="Источники данных"><thead><tr><th style="width:30%">Источник</th><th>Тип</th><th>Статус</th><th>Частота</th><th>Следующий опрос</th><th style="width:22%">Действия</th></tr></thead><tbody><tr v-if="!filtered.length && !loading && !error"><td colspan="6" class="empty-cell">Источников нет. Добавьте RSS, сайт регулятора или Telegram-канал.</td></tr><tr v-for="source in filtered" :key="source.id"><td><button class="source-name" :disabled="busy" @click="openEdit(source.id)">{{ source.name }}</button><a v-if="safeUrl(source.url)" class="source-url" :href="safeUrl(source.url)" target="_blank" rel="noopener noreferrer">{{ source.url }}</a><span v-else class="source-url">{{ source.url }}</span><span class="text-muted text-[10px]">{{ source.category }}</span></td><td>{{ SOURCE_TYPES[source.kind] || source.kind }}</td><td><span class="tag">{{ SOURCE_STATUSES[source.status] }}</span></td><td>{{ INTERVALS[source.poll_interval] || source.poll_interval }}</td><td>{{ formatDate(source.next_run_at, true) }}</td><td><div class="flex flex-wrap gap-2"><button class="text-link" :disabled="busy" @click="inspect(source.id)">История опросов</button><button v-if="source.status !== 'deleted'" class="text-link" :disabled="busy" @click="refresh(source)">Опросить</button><button v-if="source.status !== 'deleted'" role="switch" :aria-checked="source.status === 'active'" :aria-label="`Сбор данных: ${source.name}`" class="text-link" :disabled="busy" @click="action(() => api.editSource(source.id, { status: source.status === 'active' ? 'paused' : 'active' }), 'Статус источника изменён.')">{{ source.status === 'active' ? 'Пауза' : 'Включить' }}</button><button v-if="source.status === 'deleted'" class="text-link" :disabled="busy" @click="action(() => api.restoreSource(source.id), 'Источник восстановлен.')">Восстановить</button><button v-else class="text-link" :disabled="busy" @click="deleting = source; purge = false">Удалить</button></div></td></tr></tbody></table></div>
    <AppModal v-if="modal" :title="editing ? 'Редактировать источник' : 'Добавить источник'" @close="modal = false"><form @submit.prevent="save"><fieldset :disabled="busy" class="space-y-3"><label v-if="draft.type !== 'search' || editing" class="field-label">URL источника<input v-model="draft.url" class="form-control" :readonly="!!editing" required placeholder="https://" /></label><button v-if="!editing" type="button" class="outline-button" :disabled="draft.type === 'search' ? !searchQuery.trim() : !draft.url.trim()" @click="detect">Проверить источник</button><div v-if="draft.type === 'search' && !editing" class="space-y-3"><label class="field-label">Поисковый запрос<input v-model="searchQuery" required class="form-control" /></label><label class="field-label">Домены через запятую (необязательно)<input v-model="searchDomains" class="form-control" /></label><label class="field-label">Глубина поиска, дней<input v-model="searchDays" type="number" min="1" required class="form-control" /></label><p class="text-muted text-[11px]">Сохранённый поиск собирает публикации по расписанию. Для опроса требуется настроенный на сервере Tavily.</p></div><label class="field-label">Название<input v-model="draft.title" class="form-control" /></label><div class="form-grid"><label class="field-label">Тип<select v-model="draft.type" class="form-control" :disabled="!!editing"><option value="">Определить автоматически</option><option v-for="(label, key) in SOURCE_TYPES" :key="key" :value="key" :disabled="key === 'manual'">{{ label }}</option></select></label><label class="field-label">Частота опроса<select v-model="draft.poll_interval" class="form-control"><option v-for="(label, key) in INTERVALS" :key="key" :value="key">{{ label }}</option></select></label><label class="field-label">Содержание<select v-model="draft.category_hint" class="form-control"><option :value="null">Автоматически</option><option value="news">Новости</option><option value="npa">НПА</option></select></label></div><label v-if="!editing" class="flex gap-2 items-center"><input type="checkbox" :checked="draft.backfill_limit > 0" @change="draft.backfill_limit = ($event.target as HTMLInputElement).checked ? 20 : 0" />Запустить первичный сбор</label><p v-if="editing" class="unavailable">Изменение URL и типа существующего источника пока недоступно. Можно добавить новый источник.</p><div v-if="probe" class="probe-result"><strong>{{ probe.title || 'Результат проверки' }}</strong><p>Тип: {{ SOURCE_TYPES[probe.resolved_type] || probe.resolved_type }} · {{ probe.detection_method }}</p><p v-if="probe.already_exists" role="alert">Источник уже существует: #{{ probe.already_exists_source_id }}</p><p v-for="warning in probe.warnings" :key="warning">{{ warning }}</p><p>{{ probe.note }}</p><ul><li v-for="entry in probe.preview" :key="entry.url"><a v-if="safeUrl(entry.url)" class="text-link" :href="safeUrl(entry.url)" target="_blank" rel="noopener noreferrer">{{ entry.title }}</a></li></ul></div><p v-if="actionError" class="feedback-bar" role="alert">{{ actionError }}</p><div class="flex gap-2"><button type="submit" class="primary-button" :disabled="probe?.already_exists || probe?.resolved_type === 'unsupported'">{{ busy ? 'Сохранение…' : 'Сохранить источник' }}</button><button type="button" class="secondary-button" @click="modal = false">Отмена</button></div></fieldset></form></AppModal>
    <AppModal v-if="deleting" title="Удалить источник" @close="deleting = null"><p>Удалить «{{ deleting.name }}»? Исходные документы сохранятся; источник можно восстановить.</p><label class="flex items-center gap-2 my-4"><input v-model="purge" type="checkbox" />Также скрыть его материалы из ленты</label><p v-if="actionError" role="alert">{{ actionError }}</p><button class="primary-button" :disabled="busy" @click="remove">Удалить источник</button></AppModal>
    <AppModal v-if="healthId !== null" title="История опросов источника" @close="healthId = null"><p v-if="healthLoading" role="status">Загрузка…</p><p v-if="healthError" role="alert">{{ healthError }}<button class="text-link" @click="inspect(healthId!)">Повторить</button></p><template v-if="health"><h3 class="font-semibold">{{ health.source.name }}</h3><p>Документов: {{ health.documents }} · Ошибок подряд: {{ health.consecutive_failures }}</p><p>Последний успех: {{ formatDate(health.last_success_at, true) }}</p><p v-if="health.last_error" role="alert">{{ health.last_error }}</p><p v-if="!health.runs.length" class="text-muted mt-3">Опросов пока нет</p><div v-for="entry in health.runs" :key="entry.id ?? entry.started_at" class="detail-section"><p>{{ formatDate(entry.started_at, true) }} · HTTP {{ entry.http_status ?? '—' }}</p><p>Найдено: {{ entry.items_found }} · Новых: {{ entry.items_new }}</p><p v-if="entry.error_code || entry.error_message" class="text-red-600">{{ entry.error_message || entry.error_code }}</p></div></template></AppModal>
  </div>
</template>
