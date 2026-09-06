<script setup lang="ts">
import { computed, onMounted, onScopeDispose, ref, watch } from 'vue'
import { api } from '../api/client'
import type { CollectionStatus, CompanyProfile, ProcessingRun, ProcessingStatus } from '../api/types'
import { useDashboard } from '../composables/dashboard'
import { useRemote } from '../composables/remote'
import { formatDate } from '../utils/dashboard'
import AppModal from './AppModal.vue'
const store = useDashboard()
const processState = useRemote<ProcessingStatus | null>(null)
const collectionState = useRemote<CollectionStatus | null>(null)
const historyState = useRemote<ProcessingRun[]>([])
const profileState = useRemote<CompanyProfile[]>([])
const profiles = profileState.data; const profileError = profileState.error
const data = computed(() => ({ processing: processState.data.value, collection: collectionState.data.value, runs: historyState.data.value }))
const loading = computed(() => processState.loading.value || collectionState.loading.value || historyState.loading.value)
const manualRefreshing = ref(false)
const error = computed(() => [processState.error.value, collectionState.error.value, historyState.error.value].filter(Boolean).join(' · '))
const processingError = processState.error; const collectionError = collectionState.error
const clock = ref(Date.now())
const activeRun = computed(() => data.value.processing?.running || data.value.processing?.last)
const elapsed = computed(() => activeRun.value ? Math.max(0, Math.floor(((activeRun.value.finished_at ? Date.parse(activeRun.value.finished_at) : clock.value) - Date.parse(activeRun.value.started_at)) / 1000)) : 0)
const busy = ref(false); const actionError = ref(''); const message = ref('')
const interval = ref(15); const processLimit = ref(200); const useLimit = ref(false); const processSource = ref(''); const since = ref(''); const profile = ref(''); const processForce = ref(false)
const onlyFailed = ref(false)
const monitorWindow = ref(72); const collectWindow = ref(72)
const collectSource = ref(''); const dueOnly = ref(false); const backfill = ref(false); const collectForce = ref(false)
const inspected = ref<ProcessingRun | null>(null); const detailOpen = ref(false); const detailError = ref('')
const labels = { running: 'Выполняется', done: 'Завершён', failed: 'Ошибка' }
const runLabel = (run: ProcessingRun) => run.stopped ? 'Остановлен' : run.stop_requested && run.status === 'running' ? 'Останавливается' : labels[run.status]
watch(collectionState.data, (value, previous) => {
  if (value && !previous) { monitorWindow.value = value.date_window_hours; collectWindow.value = value.date_window_hours }
  if (value?.running && !previous?.running) { interval.value = value.interval_seconds / 60; monitorWindow.value = value.date_window_hours }
})
let disposed = false; let timer: ReturnType<typeof setTimeout> | undefined
async function refresh() {
  await Promise.all([
    processState.run(() => api.processing()),
    collectionState.run(() => api.collection()),
    historyState.run(async () => (await api.processingRuns()).runs),
  ])
}
async function refreshManually() {
  if (manualRefreshing.value || busy.value) return
  manualRefreshing.value = true
  try { await refresh() }
  finally { manualRefreshing.value = false }
}
watch(data, (value, previous) => {
  if (value && previous && (value.processing?.last?.finished_at !== previous.processing?.last?.finished_at || value.processing?.running?.processed !== previous.processing?.running?.processed || value.collection?.last_collect?.id !== previous.collection?.last_collect?.id)) store.changed()
})
watch(store.revision, () => { void refresh() })
watch(store.revision, () => { void profileState.run(async () => (await api.profiles()).profiles) }, { immediate: true })
async function poll() {
  if (disposed) return
  clock.value = Date.now()
  if (!document.hidden && !loading.value && !busy.value) await refresh()
  if (!disposed) timer = setTimeout(poll, 5000)
}
onMounted(() => { void refresh().then(() => { if (!disposed) timer = setTimeout(poll, 5000) }) })
onScopeDispose(() => { disposed = true; clearTimeout(timer) })
async function perform(action: () => Promise<unknown>, success: string) {
  if (busy.value) return
  busy.value = true; actionError.value = ''; message.value = ''
  try { await action(); message.value = success }
  catch (reason) { actionError.value = (reason as Error).message }
  finally { if (!disposed) { await refresh(); busy.value = false; store.changed() } }
}
function process() {
  if (data.value.processing?.running || processingError.value) return
  if (useLimit.value && (!Number.isInteger(processLimit.value) || processLimit.value < 1)) { actionError.value = 'Лимит должен быть положительным целым числом.'; return }
  void perform(async () => { const accepted = await api.startProcessing({ limit: useLimit.value ? processLimit.value : null, force: processForce.value,
    ...(onlyFailed.value ? { only_failed: true } : {}),
    ...(processSource.value ? { source_id: Number(processSource.value) } : {}),
    ...(since.value ? { since: new Date(since.value).toISOString() } : {}),
    ...(profile.value ? { profile_id: Number(profile.value) } : {}),
  }); processState.data.value = { ...processState.data.value!, running: accepted, last: accepted } }, 'Запрос на обработку принят. Результаты появятся ниже и в ленте по мере готовности.')
}
function start() {
  if (!Number.isInteger(interval.value) || interval.value < 1) { actionError.value = 'Интервал мониторинга должен быть целым числом минут, не меньше 1.'; return }
  if (!Number.isInteger(monitorWindow.value) || monitorWindow.value < 1) { actionError.value = 'Окно сбора должно быть целым числом часов, не меньше 1.'; return }
  void perform(() => api.startCollection(interval.value * 60, monitorWindow.value), 'Автоматический мониторинг запущен.')
}
function collect() {
  if (!backfill.value && (!Number.isInteger(collectWindow.value) || collectWindow.value < 1)) { actionError.value = 'Окно сбора должно быть целым числом часов, не меньше 1.'; return }
  void perform(() => api.collect({ ...(!backfill.value ? { date_window_hours: collectWindow.value } : {}), due_only: dueOnly.value, backfill: backfill.value, force: collectForce.value,
    ...(collectSource.value ? { source_ids: [Number(collectSource.value)] } : {}),
  }), 'Разовый сбор запрошен. Дождитесь обновления результата последнего цикла.')
}
function stopProcessing() {
  const run = data.value.processing?.running
  if (!run || run.stop_requested) return
  void perform(async () => {
    const result = await api.stopProcessing(run.id)
    if (processState.data.value) processState.data.value = { ...processState.data.value, running: result.status === 'running' ? result : null, last: result }
  }, 'Остановка запрошена. Текущие документы будут завершены и сохранены; новые не запускаются.')
}
async function inspect(id: number) {
  detailOpen.value = true; inspected.value = null; detailError.value = ''
  try { const result = await api.processingRun(id); if (detailOpen.value) inspected.value = result }
  catch (reason) { detailError.value = (reason as Error).message }
}
</script>
<template>
  <section class="p-4 border-b space-y-3" aria-label="Управление сбором и обработкой">
    <div class="flex items-center gap-3"><h2 class="font-semibold">Сбор и обработка</h2><button class="text-link" :disabled="manualRefreshing || busy" @click="refreshManually">Обновить состояние</button><span v-if="manualRefreshing" role="status" class="text-muted text-[11px]">Обновление…</span></div>
    <p v-if="error || actionError" role="alert" class="feedback-bar">{{ actionError || error }}</p><p v-if="message" role="status" class="feedback-bar">{{ message }}</p>
    <template v-if="data">
      <div class="form-grid">
        <section v-if="data.processing" class="border p-3 space-y-3"><h3 class="font-semibold">Очередь ИИ</h3><p>Ожидают обработки: {{ data.processing.unprocessed }} · Сбойных документов: {{ data.processing.failed }} · {{ data.processing.running ? 'Обработка выполняется' : 'Нет активного прогона' }}</p>
          <p v-if="!data.processing.llm_available" class="unavailable">Модель недоступна. Обработка сохранит базовые карточки с пометкой о неполной обработке.</p>
          <form @submit.prevent="process"><fieldset :disabled="busy || !!processingError || !!data.processing.running" class="space-y-2">
            <label class="flex gap-2"><input v-model="useLimit" type="checkbox" />Ограничить количество документов</label><p v-if="!useLimit" class="text-muted text-[11px]">Без ограничения: все подходящие документы в очереди на момент запуска.</p><label v-if="useLimit" class="field-label">Лимит обработки<input v-model.number="processLimit" type="number" min="1" step="1" required class="form-control" /></label>
            <label class="field-label">Источник обработки<select v-model="processSource" class="form-control"><option value="">Все источники</option><option v-for="source in store.filters.value?.sources ?? []" :key="source.id" :value="String(source.id)">{{ source.name }}</option></select></label>
            <details><summary class="text-link cursor-pointer">Параметры обработки</summary><div class="space-y-2 mt-2">
              <label class="field-label">Обрабатывать с даты<input v-model="since" type="datetime-local" class="form-control" /></label>
              <label class="field-label">Профиль обработки<select v-model="profile" class="form-control"><option value="">Активный профиль компании</option><option v-for="entry in profiles" :key="entry.id" :value="String(entry.id)">{{ entry.name }} · версия {{ entry.version }}{{ entry.is_default ? ' · активный' : '' }}</option></select></label><a href="#profiles" class="text-link">Управление профилями</a><p v-if="profileError" role="alert">{{ profileError }} Активный профиль по-прежнему можно использовать.</p>
              <label class="flex gap-2"><input v-model="onlyFailed" type="checkbox" />Только сбойные документы</label>
              <label class="flex gap-2"><input v-model="processForce" type="checkbox" />Повторно обработать уже обработанные документы</label>
            </div></details><button class="primary-button" type="submit">Обработать очередь ИИ</button><p class="text-muted text-[11px]">Готовые карточки сохраняются сразу. Обработку можно остановить, остальные документы останутся в очереди.</p>
          </fieldset></form>
<button v-if="data.processing.running" type="button" class="outline-button" :disabled="busy || data.processing.running.stop_requested" @click="stopProcessing">{{ data.processing.running.stop_requested ? 'Остановка запрошена' : 'Остановить обработку ИИ' }}</button><p v-if="data.processing.running?.stop_requested" role="status" class="text-muted text-[11px]">Новые запросы не запускаются. Ожидаем завершения текущих документов; результаты будут сохранены.</p>
          <div v-if="activeRun" class="unavailable" role="status" aria-label="Текущая обработка"><strong>Прогон #{{ activeRun.id }} · {{ runLabel(activeRun) }}</strong><p>Обработано документов: {{ activeRun.processed }} / {{ activeRun.documents }}<span v-if="activeRun.progress !== null"> · {{ Math.round(activeRun.progress * 100) }}%</span></p><progress v-if="activeRun.documents > 0 && activeRun.progress !== null" class="w-full" :value="activeRun.progress" max="1" aria-label="Прогресс обработки" /><p v-if="activeRun.heartbeat_at" class="text-muted text-[11px]">Последний сигнал сервера: {{ formatDate(activeRun.heartbeat_at, true) }}</p><p>Время: {{ Math.floor(elapsed / 60) }} мин. {{ elapsed % 60 }} сек. · Документов в партии: {{ activeRun.documents }}</p><p>Новых карточек: {{ activeRun.items_new }} · Объединено: {{ activeRun.items_joined }} · Обновлено: {{ activeRun.items_updated }} · Ошибок: {{ activeRun.failed }}</p><p v-if="activeRun.status === 'running'">Обработка уже запущена. Повторный запуск станет доступен после её завершения.</p><p v-if="activeRun.status === 'done' && activeRun.documents === 0">По выбранным параметрам нет документов для обработки.</p><p v-if="activeRun.error" role="alert">{{ activeRun.error }}</p></div>
        </section>
        <section v-if="data.collection" class="border p-3 space-y-3"><h3 class="font-semibold">Мониторинг источников</h3><p>{{ data.collection.running ? 'Мониторинг включён' : 'Мониторинг остановлен' }} · {{ data.collection.busy ? 'Идёт сбор' : 'Сбор свободен' }}</p><p class="text-muted">Источников к опросу: {{ data.collection.due_sources }} · Циклов: {{ data.collection.cycles }}<br />Следующая проверка: {{ formatDate(data.collection.next_tick_at, true) }}</p>
          <p v-if="data.collection.last_error" role="alert">{{ data.collection.last_error }}</p>
          <form @submit.prevent="start" class="space-y-2"><label class="field-label">Интервал мониторинга, минут<input v-model.number="interval" type="number" min="1" step="1" required :disabled="busy || data.collection.running" class="form-control" /></label><label class="field-label">Окно автоматического сбора, часов<input v-model.number="monitorWindow" type="number" min="1" step="1" required :disabled="busy || data.collection.running" class="form-control" /></label><button v-if="!data.collection.running" class="primary-button" :disabled="busy || !!collectionError">Запустить автоматический мониторинг</button><button v-else type="button" class="secondary-button" :disabled="busy || !!collectionError" @click="perform(() => api.stopCollection(), 'Автоматический мониторинг остановлен. Текущий сбор может завершаться.')">Остановить мониторинг</button></form>
          <p class="text-muted text-[11px]">Мониторинг проверяет расписания каждые {{ data.collection.interval_seconds / 60 }} мин. При сборе берутся публикации за последние {{ data.collection.date_window_hours }} ч. После перезапуска сервера его нужно включить снова. Обработка ИИ запускается отдельно.</p>
          <form @submit.prevent="collect"><fieldset :disabled="busy || !!collectionError || data.collection.busy" class="space-y-2">
            <label class="field-label">Источник разового сбора<select v-model="collectSource" class="form-control"><option value="">Все включённые источники</option><option v-for="source in store.filters.value?.sources ?? []" :key="source.id" :value="String(source.id)">{{ source.name }}</option></select></label>
            <label class="field-label">Окно разового сбора, часов<input v-model.number="collectWindow" type="number" min="1" step="1" :required="!backfill" :disabled="backfill" class="form-control" /></label><p class="text-muted text-[11px]">Публикации за последние указанное количество часов. Возможная глубина зависит от источника; публикации без даты могут также попасть в сбор.</p><label class="flex gap-2"><input v-model="dueOnly" type="checkbox" />Только источники, чья очередь пришла</label>
            <label class="flex gap-2"><input v-model="backfill" type="checkbox" />Собрать предыдущие публикации без ограничения по дате</label>
            <label class="flex gap-2"><input v-model="collectForce" type="checkbox" />Сбросить позицию предыдущего сбора</label>
            <button class="outline-button">Собрать сейчас</button>
          </fieldset></form>
          <p v-if="data.collection.last_collect" role="status">Последний цикл: {{ formatDate(data.collection.last_collect.finished_at, true) }} · Новых документов: {{ data.collection.last_collect.docs_new }} · Успешных источников: {{ data.collection.last_collect.sources_ok }} · Ошибок: {{ data.collection.last_collect.sources_fail }}</p>
        </section>
      </div>
      <details><summary class="text-link cursor-pointer">История обработки · {{ data.runs.length }}</summary><p v-if="!data.runs.length" class="text-muted">Прогонов пока нет</p><div v-for="run in data.runs" :key="run.id" class="detail-section"><button class="text-link" @click="inspect(run.id)">Прогон #{{ run.id }}</button> · {{ runLabel(run) }} · {{ formatDate(run.started_at, true) }}<p>Документов: {{ run.documents }} · Новых карточек: {{ run.items_new }} · Обновлено: {{ run.items_updated }} · Неполная обработка: {{ run.degraded }} · Ошибок: {{ run.failed }}</p><p v-if="run.error" role="alert">{{ run.error }}</p></div></details>
    </template>
    <AppModal v-if="detailOpen" title="Результат обработки" @close="detailOpen = false"><p v-if="detailError" role="alert">{{ detailError }}</p><template v-else-if="inspected"><h3>Прогон #{{ inspected.id }} · {{ runLabel(inspected) }}</h3><p>Начало: {{ formatDate(inspected.started_at, true) }} · Завершение: {{ formatDate(inspected.finished_at, true) }}</p><p>Документов: {{ inspected.documents }} · Групп: {{ inspected.clusters }} · Объединено: {{ inspected.items_joined }} · Новых: {{ inspected.items_new }} · Обновлено: {{ inspected.items_updated }}</p><p>Неполная обработка: {{ inspected.degraded }} · Ошибок: {{ inspected.failed }} · Вызовов модели: {{ inspected.calls }} · Секунд: {{ inspected.elapsed_s }}</p><p v-if="inspected.error" role="alert">{{ inspected.error }}</p><details><summary>Параметры прогона</summary><pre>{{ JSON.stringify(inspected.params, null, 2) }}</pre></details></template><p v-else>Загрузка…</p></AppModal>
  </section>
</template>
