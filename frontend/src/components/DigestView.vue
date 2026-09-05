<script setup lang="ts">
import { computed, ref } from 'vue'
import { api } from '../api/client'
import type { Digest, FeedQuery } from '../api/types'
import { useDashboard } from '../composables/dashboard'
import FilterBar from './FilterBar.vue'
import { downloadText, formatDate } from '../utils/dashboard'
const { search } = useDashboard()
const filters = ref<FeedQuery>({}); const format = ref<'markdown' | 'json'>('markdown'); const title = ref(''); const notes = ref(false)
const digest = ref<Digest | null>(null); const busy = ref(false); const error = ref(''); const draft = ref(''); const editing = ref(false)
const dateError = computed(() => filters.value.from && filters.value.to && filters.value.from > filters.value.to ? 'Начало периода не может быть позже окончания.' : '')
async function generate() {
  if (busy.value || dateError.value) return
  busy.value = true; error.value = ''
  try { const result = await api.digest({ ...filters.value, q: search.value.trim() || undefined }, format.value, title.value.trim(), notes.value); digest.value = result; draft.value = result.body; editing.value = false }
  catch (reason) { error.value = (reason as Error).message } finally { busy.value = false }
}
function exportDigest() {
  if (!digest.value) return
  if (digest.value.format === 'json') { try { JSON.parse(draft.value) } catch { error.value = 'Исправьте JSON перед экспортом.'; return } }
  downloadText(draft.value, `digest-${digest.value.generated_at.slice(0, 10)}.${digest.value.format === 'json' ? 'json' : 'md'}`, digest.value.format === 'json' ? 'application/json;charset=utf-8' : 'text/markdown;charset=utf-8')
}
function week() { const end = new Date(); const start = new Date(end); start.setDate(start.getDate() - 6); filters.value = { ...filters.value, from: start.toLocaleDateString('sv-SE'), to: end.toLocaleDateString('sv-SE') } }
</script>
<template><div class="view-column"><FilterBar v-model="filters" /><div class="toolbar gap-3 py-3"><label class="field-label">Название дайджеста<input v-model="title" class="form-control" placeholder="Название (необязательно)" /></label><label class="field-label">Формат<select v-model="format" class="form-control"><option value="markdown">Markdown</option><option value="json">JSON</option></select></label><label class="flex items-center gap-2 text-[12px]"><input v-model="notes" type="checkbox" />Включить заметки аналитика</label><button class="secondary-button" @click="week">За 7 дней</button><button class="primary-button" :disabled="busy || !!dateError" @click="generate">{{ busy ? 'Формирование…' : 'Сформировать дайджест' }}</button></div><p v-if="error || dateError" role="alert" class="feedback-bar">{{ error || dateError }}</p><div class="table-scroll p-6"><div v-if="!digest" class="empty-state"><p>Выберите период и сформируйте дайджест</p><p class="text-[12px]">Срез реальных материалов сгруппирован по приоритету. Скрытые материалы исключаются. В один экспорт входит не более 200 материалов.</p></div><template v-else><div class="flex flex-wrap items-center gap-3 mb-4"><div class="flex-1"><h2 class="font-semibold text-[15px]">{{ digest.title }}</h2><p class="text-muted text-[11px]">{{ formatDate(digest.generated_at, true) }} · {{ digest.items }} материалов · {{ digest.format }}</p></div><button class="outline-button" @click="editing = !editing">{{ editing ? 'Завершить правку' : 'Редактировать экспорт' }}</button><button class="outline-button" @click="exportDigest">Экспортировать</button></div><p v-if="digest.items >= 200" class="unavailable">Достигнут лимит 200 материалов. Уточните период или фильтры, чтобы не пропустить материалы.</p><p v-if="!digest.items" class="unavailable">За выбранный период материалов нет.</p><textarea v-if="editing" v-model="draft" class="form-control digest-editor" aria-label="Текст дайджеста" /><pre v-else class="whitespace-pre-wrap text-[13px] leading-relaxed font-sans">{{ draft }}</pre><p class="unavailable mt-4">Правки экспорта действуют в этой вкладке. Сохранение дайджеста на сервере и отправка по почте появятся позже.</p></template></div></div></template>
