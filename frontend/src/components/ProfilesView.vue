<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api/client'
import type { CompanyProfile } from '../api/types'
import { useDashboard } from '../composables/dashboard'
import { useRemote } from '../composables/remote'
import { formatDate } from '../utils/dashboard'
const store = useDashboard()
const { data, error, loading, run } = useRemote<{ profiles: CompanyProfile[]; active: CompanyProfile } | null>(null)
const fields = [
  { key: 'industry', label: 'Отрасль', list: false },
  { key: 'products', label: 'Продукты', list: true },
  { key: 'stack', label: 'Технологии и направления', list: true },
  { key: 'regime', label: 'Налоговый и аккредитационный режим', list: false },
  { key: 'regulators', label: 'Регуляторы', list: true },
  { key: 'competitors', label: 'Конкуренты', list: true },
  { key: 'topics', label: 'Ключевые темы', list: true },
  { key: 'negative_facets', label: 'Не относится к компании', list: true },
]
const selected = ref<CompanyProfile | null>(null); const editorOpen = ref(false)
const name = ref(''); const values = ref<Record<string, string>>({}); let original: Record<string, string> = {}
const busy = ref(false); const actionError = ref(''); const message = ref('')
const shown = computed(() => data.value?.profiles.filter(profile => profile.name.toLocaleLowerCase().includes(store.search.value.trim().toLocaleLowerCase())) ?? [])
async function refresh() {
  await run(async () => { const active = await api.activeProfile(); const { profiles } = await api.profiles(); return { active, profiles } })
}
function edit(profile: CompanyProfile | null) {
  selected.value = profile; name.value = profile?.name ?? ''; editorOpen.value = true; actionError.value = ''; message.value = ''
  values.value = Object.fromEntries(fields.map(field => { const value = profile?.payload[field.key]; return [field.key, Array.isArray(value) ? value.join('\n') : value == null ? '' : String(value)] }))
  original = { ...values.value }
}
async function open(id: number) {
  if (busy.value) return
  busy.value = true; actionError.value = ''
  try { edit(await api.profile(id)) } catch (reason) { actionError.value = (reason as Error).message }
  finally { busy.value = false }
}
async function save() {
  if (busy.value) return
  actionError.value = ''; message.value = ''
  if (!name.value.trim()) { actionError.value = 'Укажите название профиля.'; return }
  if (!selected.value && data.value?.profiles.some(profile => profile.name === name.value.trim())) { actionError.value = 'Профиль с таким названием уже есть. Откройте его для редактирования.'; return }
  const payload = { ...selected.value?.payload }
  for (const field of fields) {
    if (selected.value && values.value[field.key] === original[field.key]) continue
    const value = values.value[field.key]?.trim() ?? ''
    if (value) payload[field.key] = field.list ? [...new Set(value.split('\n').map(entry => entry.trim()).filter(Boolean))] : value
    else delete payload[field.key]
  }
  if (!Object.keys(payload).length) { actionError.value = 'Заполните хотя бы одно поле профиля.'; return }
  busy.value = true
  try {
    const saved = await api.saveProfile(name.value.trim(), payload)
    edit(saved); await refresh()
    message.value = `Профиль сохранён · версия ${saved.version}.`; store.changed()
  } catch (reason) { actionError.value = (reason as Error).message }
  finally { busy.value = false }
}
async function activate(id: number) {
  if (busy.value) return
  busy.value = true; actionError.value = ''; message.value = ''
  try { const active = await api.activateProfile(id); await refresh(); message.value = `Активный профиль: ${active.name}. Новые прогоны будут использовать его.`; store.changed() }
  catch (reason) { actionError.value = (reason as Error).message }
  finally { busy.value = false }
}
onMounted(() => { void refresh() })
</script>
<template>
  <div class="view-column">
    <div class="toolbar gap-3 py-3"><div class="flex-1"><h2 class="font-semibold">Профили компании</h2><p class="text-muted text-[12px]">Определяют, какие новости и НПА важны для вашего бизнеса.</p></div><button class="outline-button" :disabled="busy" @click="edit(null)">Создать профиль</button><button class="text-link" :disabled="loading || busy" @click="refresh">Обновить профили</button></div>
    <p v-if="error || actionError" role="alert" class="feedback-bar">{{ actionError || error }}</p><p v-if="message" role="status" class="feedback-bar">{{ message }}</p>
    <div class="table-scroll p-4 space-y-4">
      <p v-if="loading && !data" role="status">Загрузка профилей…</p>
      <p v-if="data" class="unavailable">Активный профиль: <strong>{{ data.active.name }}</strong> · версия {{ data.active.version }}. Изменения применяются к будущей обработке; готовые карточки сохраняют свою версию профиля.</p>
      <div class="space-y-2"><article v-for="profile in shown" :key="profile.id" class="border p-3 flex flex-wrap items-center gap-3"><div class="flex-1"><button class="text-link" :disabled="busy" @click="open(profile.id)">{{ profile.name }}</button><p class="text-muted text-[11px]">Версия {{ profile.version }} · {{ formatDate(profile.updated_at, true) }}</p></div><span v-if="profile.id === data?.active.id" class="tag">Активный</span><button v-else class="outline-button" :disabled="busy" @click="activate(profile.id)">Использовать {{ profile.name }}</button></article><p v-if="data && !shown.length" class="text-muted">Профили не найдены.</p></div>
      <form v-if="editorOpen" class="border p-4 space-y-3" @submit.prevent="save"><h3 class="font-semibold">{{ selected ? 'Редактирование профиля' : 'Новый профиль' }}</h3><fieldset :disabled="busy" class="space-y-3">
        <label class="field-label">Название профиля<input v-model="name" required maxlength="200" :readonly="!!selected" class="form-control" /></label>
        <p class="text-muted text-[11px]">Сведения профиля передаются модели при обработке. В списках указывайте по одному пункту на строку.</p>
        <div class="form-grid"><label v-for="field in fields" :key="field.key" class="field-label">{{ field.label }}<textarea v-model="values[field.key]" class="form-control" :rows="field.list ? 3 : 2" /></label></div>
        <div class="flex gap-3"><button class="primary-button">{{ busy ? 'Сохранение…' : 'Сохранить профиль' }}</button><button type="button" class="text-link" @click="editorOpen = false">Закрыть редактор</button></div>
      </fieldset></form>
    </div>
  </div>
</template>
