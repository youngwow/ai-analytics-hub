<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api } from '../api/client'
import type { Facets, Feed, FeedQuery, ManualResult } from '../api/types'
import { useDashboard } from '../composables/dashboard'
import { useRemote } from '../composables/remote'
import { PRIORITIES, VISIBILITY } from '../data/dashboard'
import { downloadText, formatDate, safeUrl, splitTags } from '../utils/dashboard'
import FilterBar from './FilterBar.vue'
import ItemPanel from './ItemPanel.vue'
import ManualItemForm from './ManualItemForm.vue'
const props = defineProps<{ npa?: boolean }>()
const store = useDashboard(); const { search, revision } = store
const filters = ref<FeedQuery>({}); const includeHidden = ref(false)
const selected = ref<number[]>([]); const selectedId = ref<number | null>(null); const adding = ref(false)
const actionBusy = ref(false); const actionError = ref(''); const actionMessage = ref(''); const reason = ref('')
const addTags = ref(''); const removeTags = ref(''); const exporting = ref(false)
const query = computed<FeedQuery>(() => ({ ...filters.value, q: search.value.trim() || undefined, type: props.npa ? 'npa' : filters.value.type, npa_status: props.npa || filters.value.type === 'npa' ? filters.value.npa_status : undefined, limit: 20, include_hidden: includeHidden.value }))
const dateError = computed(() => query.value.from && query.value.to && query.value.from > query.value.to ? 'Начало периода не может быть позже окончания.' : '')
const { data, loading, error, run } = useRemote<{ feed: Feed; facets: Facets | null }>({ feed: { items: [], total: 0, next_cursor: null, took_ms: 0 }, facets: null })
function load(more = false) {
  if (dateError.value) return
  const current = query.value
  if (!more) { selected.value = []; data.value = { feed: { items: [], total: 0, next_cursor: null, took_ms: 0 }, facets: null } }
  const previous = data.value
  void run(async signal => {
    const [feed, facets] = await Promise.all([api.feed({ ...current, cursor: more ? previous.feed.next_cursor ?? undefined : undefined }, signal), api.facets(current, signal)])
    return { feed: { ...feed, items: more ? [...previous.feed.items, ...feed.items] : feed.items }, facets }
  })
}
watch([query, revision], () => load(), { immediate: true })
function created(result: ManualResult) {
  adding.value = false
  actionMessage.value = result.processing_status === 'queued'
    ? 'Документ сохранён и ожидает обработки. Его можно найти в разделе «Сбор и обработка».'
    : 'Материал сохранён на сервере. Если он не виден, проверьте фильтры ленты.'
  store.changed()
}
async function bulk(scope: 'feed' | 'digest' | 'visible') {
  await changeSelected(() => api.bulk([...selected.value], scope, reason.value))
}
async function changeSelected(action: () => Promise<{ changed: number }>) {
  if (!selected.value.length || actionBusy.value) return
  actionBusy.value = true; actionError.value = ''; actionMessage.value = ''
  try { const result = await action(); actionMessage.value = `Обновлено материалов: ${result.changed}`; addTags.value = ''; removeTags.value = ''; store.changed() }
  catch (error) { actionError.value = (error as Error).message } finally { actionBusy.value = false }
}
function tagSelected() {
  const add = splitTags(addTags.value); const remove = splitTags(removeTags.value)
  if (!add.length && !remove.length) { actionError.value = 'Укажите теги для добавления или удаления.'; return }
  if (add.some(tag => remove.includes(tag))) { actionError.value = 'Один тег нельзя одновременно добавить и удалить.'; return }
  void changeSelected(() => api.bulkTags([...selected.value], add, remove))
}
async function exportFeed(format: 'csv' | 'rss') {
  if (exporting.value || dateError.value) return
  exporting.value = true; actionError.value = ''; actionMessage.value = ''
  try {
    const body = await api.exportFeed(format, query.value)
    downloadText(body, `feed-${new Date().toISOString().slice(0, 10)}.${format === 'rss' ? 'xml' : 'csv'}`, format === 'rss' ? 'application/rss+xml;charset=utf-8' : 'text/csv;charset=utf-8')
    actionMessage.value = 'Экспорт готов: до 200 видимых неархивных материалов по текущим фильтрам. Заметки аналитика не включены.'
  } catch (reason) { actionError.value = (reason as Error).message }
  finally { exporting.value = false }
}
</script>
<template>
  <div class="view-column">
    <FilterBar v-model="filters" :npa="npa" archive />
    <details class="px-4 py-2 border-b"><summary class="text-link cursor-pointer">Экспорт ленты</summary><div class="flex flex-wrap items-center gap-3 py-2"><button class="outline-button" :disabled="exporting || !!dateError" @click="exportFeed('csv')">Скачать CSV</button><button class="outline-button" :disabled="exporting || !!dateError" @click="exportFeed('rss')">Скачать RSS</button><span class="text-muted text-[11px]">До 200 видимых неархивных материалов по текущим фильтрам, без заметок аналитика.</span></div></details>
    <div class="toolbar gap-3 py-2"><label class="text-[11px] flex items-center gap-2"><input v-model="includeHidden" type="checkbox" />Показать скрытые и удалённые</label><span v-if="data.facets" class="text-[11px] text-muted">Высокий: {{ data.facets.by_priority.high ?? 0 }} · Средний: {{ data.facets.by_priority.medium ?? 0 }} · Низкий: {{ data.facets.by_priority.low ?? 0 }}</span><span class="ml-auto text-[11px]" role="status">{{ data.feed.total }} материалов</span><button class="outline-button" @click="adding = true">+ Добавить {{ npa ? 'НПА' : 'материал' }}</button><button class="text-link" :disabled="loading" @click="load()">Обновить</button></div>
    <div v-if="selected.length" class="toolbar gap-2 py-2"><span>Выбрано: {{ selected.length }}</span><input v-model="reason" class="form-control bulk-reason" aria-label="Причина скрытия" placeholder="Причина (необязательно)" /><button class="secondary-button" :disabled="actionBusy" @click="bulk('digest')">Исключить из дайджеста</button><button class="secondary-button" :disabled="actionBusy" @click="bulk('feed')">Скрыть из ленты</button><button class="secondary-button" :disabled="actionBusy" @click="bulk('visible')">Вернуть в ленту</button></div>
    <div v-if="selected.length" class="toolbar gap-3 py-2"><button class="secondary-button" :disabled="actionBusy" @click="changeSelected(() => api.bulkArchive([...selected], true))">Архивировать выбранные</button><button class="secondary-button" :disabled="actionBusy" @click="changeSelected(() => api.bulkArchive([...selected], false))">Вернуть выбранные из архива</button><form class="flex flex-wrap gap-2 items-end" @submit.prevent="tagSelected"><label class="field-label">Добавить теги<input v-model="addTags" :disabled="actionBusy" class="form-control" placeholder="Через запятую" /></label><label class="field-label">Снять теги<input v-model="removeTags" :disabled="actionBusy" class="form-control" placeholder="Через запятую" /></label><button class="outline-button" :disabled="actionBusy">Применить теги</button></form></div>
    <p v-if="dateError || error || actionError" class="feedback-bar" role="alert">{{ dateError || error || actionError }}<button v-if="!dateError" class="text-link" @click="load()">Повторить</button></p><p v-if="actionMessage" class="feedback-bar" role="status">{{ actionMessage }}</p>
    <div class="table-scroll" :aria-busy="loading">
      <p v-if="loading && !data.feed.items.length" class="empty-state" role="status">Загрузка материалов…</p>
      <div v-else-if="!data.feed.items.length && !error" class="empty-state"><p>{{ npa ? 'НПА не найдены' : 'Материалов пока нет' }}</p><p class="text-[12px]">Измените фильтры, добавьте материал или подключите источники.</p></div>
      <table v-if="npa && data.feed.items.length" class="data-table npa-table" aria-label="Реестр НПА"><thead><tr><th>Выбор</th><th>Статус</th><th style="width:40%">Документ</th><th>Источник</th><th>Опубликован</th><th>Приоритет</th></tr></thead><tbody><tr v-for="item in data.feed.items" :key="item.id"><td><input v-model="selected" type="checkbox" :value="item.id" :aria-label="`Выбрать материал ${item.id}`" /></td><td><span class="tag">{{ item.npa_status || 'Статус не указан' }}</span></td><td><button class="source-name" @click="selectedId = item.id">{{ item.title || 'Без заголовка' }}</button><span class="text-muted ml-2">#{{ item.id }}</span><p class="text-muted text-[12px]">{{ item.summary }}</p><span v-for="tag in item.tags" :key="tag" class="tag mr-1">{{ tag }}</span><p v-if="item.visibility !== 'visible'" class="text-muted text-[10px]">{{ VISIBILITY[item.visibility] }}</p></td><td>{{ item.source_name || 'Не указан' }}</td><td>{{ formatDate(item.published_at) }}</td><td>{{ PRIORITIES[item.priority] ?? item.priority }}</td></tr></tbody></table>
      <template v-if="!npa"><article v-for="item in data.feed.items" :key="item.id" class="news-row group" :aria-label="item.title || 'Материал'">
        <div class="news-priority"><span class="priority-badge" :style="{ background: `var(--priority-${item.priority}-bg)`, color: `var(--priority-${item.priority}-text)` }">{{ PRIORITIES[item.priority] ?? item.priority }}</span><input v-model="selected" type="checkbox" class="block mt-3" :value="item.id" :aria-label="`Выбрать материал ${item.id}`" /></div>
        <div class="flex-1 min-w-0"><div class="flex flex-wrap gap-2 text-[11px] text-muted mb-1"><span>#{{ item.id }} · {{ item.source_name || 'Источник не указан' }}</span><span v-if="item.sources_count && item.sources_count > 1">{{ item.sources_count }} публикаций в группе</span><time class="ml-auto">{{ formatDate(item.published_at, true) }}</time></div><h2><button class="source-name" @click="selectedId = item.id">{{ item.title || 'Без заголовка' }}</button></h2><p class="text-[12px] leading-relaxed text-muted mt-1">{{ item.summary || 'Саммари ещё не подготовлено' }}</p><div class="flex gap-2 flex-wrap mt-2"><span v-for="tag in item.tags" :key="tag" class="tag">{{ tag }}</span><span v-if="item.type === 'npa'" class="tag">НПА · {{ item.npa_status || 'статус не указан' }}</span><span v-if="item.flags.degraded" class="tag">Обработка неполная</span><span v-if="item.flags.needs_review" class="tag">Нужна проверка</span><span v-if="item.flags.date_estimated" class="tag">Дата оценена</span><span v-if="item.flags.edited" class="tag">Правка аналитика</span><span v-if="item.flags.duplicate" class="tag">Вероятный дубль</span><span v-if="item.visibility !== 'visible'" class="tag">{{ VISIBILITY[item.visibility] }}</span></div><div class="flex gap-3 mt-2"><button class="text-link" @click="selectedId = item.id">Карточка и редактирование</button><a v-if="safeUrl(item.canonical_url)" class="text-link" :href="safeUrl(item.canonical_url)" target="_blank" rel="noopener noreferrer">Оригинал ↗</a></div></div>
      </article></template>
      <div v-if="data.feed.next_cursor" class="p-4 text-center"><button class="outline-button" :disabled="loading" @click="load(true)">{{ loading ? 'Загрузка…' : 'Загрузить ещё' }}</button></div>
    </div>
    <ItemPanel v-if="selectedId !== null" :id="selectedId" @close="selectedId = null" @changed="store.changed()" />
    <ManualItemForm v-if="adding" :npa="npa" @close="adding = false" @created="created" />
  </div>
</template>
