<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api } from '../api/client'
import type { Documents, FeedQuery } from '../api/types'
import { useDashboard } from '../composables/dashboard'
import { useRemote } from '../composables/remote'
import { safeUrl, formatDate } from '../utils/dashboard'
import FilterBar from './FilterBar.vue'
import WorkerControls from './WorkerControls.vue'
const store = useDashboard(); const { status, health, search, revision } = store
const filters = ref<FeedQuery>({}); const processHealth = ref('')
const { data, loading, error, run } = useRemote<Documents>({ documents: [], total: 0, next_cursor: null, took_ms: 0 })
const dateError = computed(() => filters.value.from && filters.value.to && filters.value.from > filters.value.to ? 'Начало периода не может быть позже окончания.' : '')
function load(more = false) {
  if (dateError.value) return
  const previous = data.value
  const query = { ...filters.value, q: search.value.trim() || undefined, limit: 20, cursor: more ? previous.next_cursor ?? undefined : undefined }
  if (!more) data.value = { documents: [], total: 0, next_cursor: null, took_ms: 0 }
  void run(async signal => { const result = await api.documents(query, signal); return { ...result, documents: more ? [...previous.documents, ...result.documents] : result.documents } })
}
watch([filters, search, revision], () => load(), { immediate: true, deep: true })
async function check() { try { const result = await api.health(); processHealth.value = `${result.app} · ${result.version} · ${result.status}` } catch (reason) { processHealth.value = (reason as Error).message } }
</script>
<template><div class="view-column status-view"><div class="toolbar gap-5 py-4"><div><strong>{{ status?.documents ?? '—' }}</strong><p class="text-muted text-[11px]">Собрано документов</p></div><div><strong>{{ status?.items ?? '—' }}</strong><p class="text-muted text-[11px]">Обработано материалов</p></div><div><strong>{{ status?.unprocessed ?? '—' }}</strong><p class="text-muted text-[11px]">Ожидают обработки</p></div><div class="text-[11px] text-muted">Последний сбор: {{ formatDate(status?.last_collect_at, true) }}<br />Готовность: {{ health?.status ?? 'нет данных' }} · Часовой пояс: {{ status?.timezone ?? '—' }}</div><button class="text-link" @click="check">Проверить процесс</button><span role="status">{{ processHealth }}</span></div><dl v-if="health" class="toolbar py-2 gap-4" aria-label="Проверки готовности"><div v-for="(value, name) in health.checks" :key="name"><dt class="text-muted text-[11px]">{{ name === 'repository' ? 'Хранилище' : name }}</dt><dd>{{ value === 'ok' ? 'Доступно' : value === 'unavailable' ? 'Недоступно' : value === 'error' ? 'Ошибка проверки' : value }}</dd></div></dl><WorkerControls /><FilterBar v-model="filters" documents /><div class="toolbar py-2 gap-3"><h2 class="font-semibold">Собрано, но ещё не обработано</h2><span class="ml-auto text-muted">{{ data.total }} документов</span><button class="text-link" :disabled="loading" @click="load()">Обновить очередь</button></div><p v-if="error || dateError" role="alert" class="feedback-bar">{{ error || dateError }}</p><div class="table-scroll"><p v-if="loading" role="status" class="p-4">Загрузка очереди…</p><p v-if="!data.documents.length && !loading && !error" class="empty-state">Необработанных документов нет</p><table v-if="data.documents.length" class="data-table" aria-label="Необработанные документы"><thead><tr><th>Документ</th><th>Источник</th><th>Опубликован</th><th>Символов</th></tr></thead><tbody><tr v-for="document in data.documents" :key="document.id"><td><a v-if="safeUrl(document.url)" class="text-link" :href="safeUrl(document.url)" target="_blank" rel="noopener noreferrer">{{ document.title || document.url }}</a><span v-else>{{ document.title || 'Без заголовка' }}</span></td><td>{{ document.source_name || '—' }}</td><td>{{ formatDate(document.published_at) }}</td><td>{{ document.chars ?? '—' }}</td></tr></tbody></table><div v-if="data.next_cursor" class="p-4 text-center"><button class="outline-button" :disabled="loading" @click="load(true)">Загрузить ещё документы</button></div><section v-if="status?.stale_sources.length" class="p-5"><h3 class="font-semibold mb-2">Источники с задержкой опроса</h3><p v-for="source in status.stale_sources" :key="source.id" class="py-1">{{ source.name }} · {{ source.overdue_minutes }} мин. · {{ source.last_error || 'Ожидает опроса' }}</p></section></div></div></template>
