<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api/client'
import type { Quality } from '../api/types'
import { useRemote } from '../composables/remote'
import { PRIORITIES } from '../data/dashboard'
const { data, loading, error, run } = useRemote<Quality | null>(null)
const since = ref(''); const until = ref('')
const dateError = computed(() => since.value && until.value && since.value > until.value ? 'Начало периода не может быть позже окончания.' : '')
function refresh() {
  if (dateError.value) return
  void run(signal => api.quality({ since: since.value ? new Date(since.value).toISOString() : undefined, until: until.value ? new Date(until.value).toISOString() : undefined }, signal))
}
const seconds = (ms: number) => (ms / 1000).toLocaleString('ru-RU', { maximumFractionDigits: 1 })
const callStatus = (status: string) => ({ ok: 'Успешно', failed: 'Ошибка', retry: 'Повтор' })[status] ?? status
onMounted(refresh)
</script>
<template>
  <details class="p-4 border-b space-y-3" aria-label="Качество модели">
    <summary class="font-semibold cursor-pointer">Качество модели</summary>
    <form class="flex flex-wrap items-end gap-3" @submit.prevent="refresh">
      <label class="field-label">Вызовы с даты<input v-model="since" type="datetime-local" class="form-control" /></label>
      <label class="field-label">Вызовы до даты<input v-model="until" type="datetime-local" class="form-control" /></label>
      <button class="outline-button" :disabled="loading || !!dateError">{{ loading ? 'Загрузка качества…' : 'Обновить качество' }}</button>
    </form>
    <p v-if="error || dateError" role="alert">{{ error || dateError }}</p>
    <template v-if="data">
      <div class="form-grid">
        <section class="border p-3 space-y-2"><h3 class="font-semibold">Текущее состояние карточек</h3>
          <p>Всего: {{ data.items }} · Неполная обработка: {{ data.degraded }} · Нужна проверка: {{ data.hallucination_flags }}</p>
          <p><span v-for="(label, key) in PRIORITIES" :key="key" class="mr-3">{{ label }}: {{ data.by_priority[key] ?? 0 }}</span></p>
          <p>Очередь: {{ data.queue.unprocessed }} · Сбойных документов: {{ data.queue.failed }}</p>
          <p>Доля карточек с правками{{ since ? ' с начала периода' : '' }}: {{ (data.edited_share * 100).toLocaleString('ru-RU', { maximumFractionDigits: 1 }) }}%</p>
          <p class="text-muted text-[11px]">Счётчики карточек и очереди показывают текущее состояние. Это показатели обработки и проверки, а не оценка точности модели.</p>
        </section>
        <section class="border p-3 space-y-2"><h3 class="font-semibold">Вызовы модели за период</h3>
          <p>Вызовов: {{ data.calls }} · Ошибок: {{ data.failed_calls }}</p>
          <p>Среднее время вызова: {{ seconds(data.avg_latency_ms) }} сек.</p>
          <p>Входных токенов: {{ data.tokens_in.toLocaleString('ru-RU') }} · Выходных: {{ data.tokens_out.toLocaleString('ru-RU') }}</p>
          <p v-if="!data.calls" class="text-muted">За этот период вызовов модели нет.</p>
        </section>
      </div>
      <div v-if="data.by_stage.length" class="overflow-x-auto"><table class="data-table" aria-label="Вызовы модели по этапам"><thead><tr><th>Этап</th><th>Результат</th><th>Вызовы</th><th>Среднее время, сек.</th><th>Входные / выходные токены</th></tr></thead><tbody><tr v-for="row in data.by_stage" :key="`${row.stage}-${row.status}`"><td>{{ row.stage === 's2_s5' ? 'Анализ и саммаризация' : row.stage }}</td><td>{{ callStatus(row.status) }}</td><td>{{ row.calls }}</td><td>{{ seconds(row.avg_latency_ms) }}</td><td>{{ row.tokens_in }} / {{ row.tokens_out }}</td></tr></tbody></table></div>
      <div v-if="data.by_day.length" class="overflow-x-auto"><table class="data-table" aria-label="Вызовы модели по дням"><thead><tr><th>Дата</th><th>Вызовы</th><th>Ошибки</th><th>Входные / выходные токены</th></tr></thead><tbody><tr v-for="row in data.by_day" :key="row.day"><td>{{ row.day }}</td><td>{{ row.calls }}</td><td>{{ row.failed }}</td><td>{{ row.tokens_in }} / {{ row.tokens_out }}</td></tr></tbody></table></div>
    </template>
  </details>
</template>
