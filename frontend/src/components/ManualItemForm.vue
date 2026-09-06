<script setup lang="ts">
import { ref } from 'vue'
import AppModal from './AppModal.vue'
import { api, ApiError } from '../api/client'
import type { ItemCreate, ManualResult } from '../api/types'
import { NPA_STATUSES, TYPES } from '../data/dashboard'
import { safeUrl } from '../utils/dashboard'
const props = defineProps<{ npa?: boolean }>(); const emit = defineEmits<{ close: []; created: [ManualResult] }>()
const draft = ref<ItemCreate>({ title: '', url: '', raw_text: '', type: props.npa ? 'npa' : 'news', run_llm: true, force: false })
const date = ref(''); const npaStatus = ref('анонс'); const busy = ref(false); const error = ref(''); const duplicate = ref(false)
async function save() {
  if (busy.value) return
  error.value = ''
  if (!draft.value.title.trim() && !draft.value.url.trim()) { error.value = 'Укажите заголовок или ссылку.'; return }
  if (draft.value.url.trim() && !safeUrl(draft.value.url.trim())) { error.value = 'Укажите HTTP(S) ссылку на оригинал.'; return }
  busy.value = true
  try { const result = await api.createItem({ ...draft.value, title: draft.value.title.trim(), url: draft.value.url.trim(), ...(date.value ? { published_at: new Date(date.value).toISOString() } : {}), ...(draft.value.type === 'npa' ? { npa_status: npaStatus.value } : {}) }); emit('created', result) }
  catch (reason) { error.value = (reason as Error).message; duplicate.value = reason instanceof ApiError && reason.code === 'possible_duplicate'; if (!duplicate.value) draft.value.force = false } finally { busy.value = false }
}
</script>
<template><AppModal title="Добавить материал вручную" @close="emit('close')"><form @submit.prevent="save"><fieldset :disabled="busy" class="space-y-3"><p class="text-muted text-[12px]">Добавьте ссылку или заголовок. Для документа из письма или закрытого чата вставьте текст.</p><label class="field-label">Заголовок<input v-model="draft.title" class="form-control" /></label><label class="field-label">Ссылка на оригинал<input v-model="draft.url" class="form-control" placeholder="https://" /></label><label class="field-label">Текст материала<textarea v-model="draft.raw_text" rows="6" class="form-control" /></label><div class="form-grid"><label class="field-label">Тип<select v-model="draft.type" class="form-control"><option v-for="(label, key) in TYPES" :key="key" :value="key">{{ label }}</option></select></label><label v-if="draft.type === 'npa'" class="field-label">Статус НПА<select v-model="npaStatus" class="form-control"><option v-for="status in NPA_STATUSES" :key="status">{{ status }}</option></select></label><label class="field-label">Дата публикации<input v-model="date" type="datetime-local" class="form-control" /></label></div><label class="flex gap-2 items-center text-[12px]"><input v-model="draft.run_llm" type="checkbox" />Подготовить саммари с помощью ИИ</label><p class="text-muted text-[11px]">Если модель недоступна, сервер сохранит материал с пометкой о неполной обработке.</p><p v-if="error" role="alert" class="feedback-bar">{{ error }}</p><label v-if="duplicate" class="flex gap-2 text-[12px]"><input v-model="draft.force" type="checkbox" />Создать отдельно, несмотря на возможный дубль</label><div class="flex gap-2"><button class="primary-button" type="submit">{{ busy ? 'Сохранение…' : 'Создать материал' }}</button><button class="secondary-button" type="button" @click="emit('close')">Отмена</button></div></fieldset></form></AppModal></template>
