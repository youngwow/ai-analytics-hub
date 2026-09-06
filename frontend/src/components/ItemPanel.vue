<script setup lang="ts">
import { ref, watch } from 'vue'
import { api } from '../api/client'
import type { ItemCard, ItemType, ItemUpdate, Priority } from '../api/types'
import { useRemote } from '../composables/remote'
import { CATEGORIES, EDIT_REASONS, ENTITY_ROLES, NPA_STATUSES, PRIORITIES, TYPES, VISIBILITY } from '../data/dashboard'
import { formatDate, safeUrl, splitTags } from '../utils/dashboard'
import AppModal from './AppModal.vue'
const props = defineProps<{ id: number }>(); const emit = defineEmits<{ close: []; changed: [] }>()
const { data: card, loading, error, run } = useRemote<ItemCard | null>(null)
const editing = ref(false); const busy = ref(false); const actionError = ref(''); const message = ref('')
const draft = ref({ title: '', summary: '', type: 'news' as ItemType, npa_status: '', priority: 'medium' as Priority, tags: '', category: '', edit_reason: 'other' })
const note = ref(''); const author = ref(''); const reason = ref('')
const eventOpen = ref(false)
const eventDraft = ref({ status: '', date: '', source_url: '', note: '' })
function addEvent() {
  if (!eventDraft.value.status.trim()) { actionError.value = 'Укажите статус или название события.'; return }
  if (eventDraft.value.source_url.trim() && !safeUrl(eventDraft.value.source_url.trim())) { actionError.value = 'Укажите HTTP(S) ссылку события.'; return }
  void perform(() => api.addEvent(props.id, {
    status: eventDraft.value.status.trim(), source_url: eventDraft.value.source_url.trim(), note: eventDraft.value.note.trim(),
    ...(eventDraft.value.date ? { occurred_at: new Date(eventDraft.value.date).toISOString() } : {}),
  }), 'Событие сохранено. Текущий статус получен с сервера.', () => { eventOpen.value = false; eventDraft.value = { status: '', date: '', source_url: '', note: '' } })
}
function load() { return run(() => api.card(props.id)) }
watch(() => props.id, () => { editing.value = false; void load() }, { immediate: true })
function edit() {
  if (!card.value) return
  const item = card.value.item
  const category = item.tags.find(tag => CATEGORIES.includes(tag)) || ''
  draft.value = { title: item.title || '', summary: item.summary || '', type: item.type, npa_status: item.npa_status || 'анонс', priority: item.priority, tags: item.tags.filter(tag => tag !== category).join(', '), category, edit_reason: 'other' }
  editing.value = true
}
async function perform(action: () => Promise<unknown>, success: string, after?: () => void) {
  if (busy.value) return
  busy.value = true; actionError.value = ''; message.value = ''
  try { await action(); message.value = success; after?.(); emit('changed'); await load() }
  catch (reason) { actionError.value = (reason as Error).message } finally { busy.value = false }
}
function save() {
  if (!card.value) return
  if (!draft.value.title.trim() || !draft.value.summary.trim()) { actionError.value = 'Заголовок и саммари не могут быть пустыми.'; return }
  const body: ItemUpdate = { title: draft.value.title.trim(), summary: draft.value.summary.trim(), priority: draft.value.priority, type: draft.value.type, tags: [...new Set([draft.value.category, ...splitTags(draft.value.tags)].filter(Boolean))], edit_reason: draft.value.edit_reason }
  if (body.type === 'npa') body.npa_status = draft.value.npa_status
  const item = card.value.item
  const changed = Object.fromEntries(Object.entries(body).filter(([key, value]) => {
    if (key === 'edit_reason') return false
    if (key === 'tags') return JSON.stringify([...item.tags].sort()) !== JSON.stringify([...(value as string[])].sort())
    return value !== item[key as keyof typeof item]
  })) as ItemUpdate
  if (!Object.keys(changed).length) { editing.value = false; message.value = 'Нет изменений для сохранения.'; return }
  void perform(() => api.editItem(props.id, { ...changed, edit_reason: draft.value.edit_reason }), 'Изменения сохранены на сервере.', () => { editing.value = false })
}
async function revisions() {
  if (!card.value || busy.value) return
  busy.value = true; actionError.value = ''
  try { card.value = { ...card.value, revisions: (await api.revisions(props.id)).revisions } } catch (reason) { actionError.value = (reason as Error).message } finally { busy.value = false }
}
</script>
<template>
  <AppModal title="Карточка материала" @close="emit('close')">
    <p v-if="loading" role="status">Загрузка карточки…</p><p v-if="error" role="alert" class="feedback-bar">{{ error }}<button class="text-link" @click="load">Повторить</button></p><p v-if="actionError" role="alert" class="feedback-bar">{{ actionError }}</p><p v-if="message" role="status" class="feedback-bar">{{ message }}</p>
    <template v-if="card">
      <div class="flex gap-2 flex-wrap text-[11px] text-muted mb-3"><span>#{{ id }}</span><span>{{ TYPES[card.item.type] }}</span><span>{{ VISIBILITY[card.item.visibility] }}</span><span>{{ formatDate(card.item.published_at, true) }}</span><a v-if="safeUrl(card.canonical_url)" class="text-link" :href="safeUrl(card.canonical_url)" target="_blank" rel="noopener noreferrer">Оригинал ↗</a></div>
      <form v-if="editing" @submit.prevent="save" @keydown.esc.stop="editing = false"><fieldset :disabled="busy || loading" class="space-y-3">
        <label class="field-label">Заголовок<input v-model="draft.title" required class="form-control" /></label><label class="field-label">Саммари<textarea v-model="draft.summary" required rows="5" class="form-control" /></label>
        <div class="form-grid"><label class="field-label">Тип<select v-model="draft.type" class="form-control"><option v-for="(label, key) in TYPES" :key="key" :value="key">{{ label }}</option></select></label><label class="field-label">Приоритет<select v-model="draft.priority" class="form-control"><option v-for="(label, key) in PRIORITIES" :key="key" :value="key">{{ label }}</option></select></label><label v-if="draft.type === 'npa'" class="field-label">Статус НПА<select v-model="draft.npa_status" class="form-control"><option v-for="status in NPA_STATUSES" :key="status">{{ status }}</option></select></label><label class="field-label">Категория<select v-model="draft.category" class="form-control"><option value="">Не указана</option><option v-for="category in CATEGORIES" :key="category">{{ category }}</option></select></label></div>
        <label class="field-label">Дополнительные теги через запятую<input v-model="draft.tags" class="form-control" /></label><label class="field-label">Причина правки<select v-model="draft.edit_reason" class="form-control"><option v-for="(label, key) in EDIT_REASONS" :key="key" :value="key">{{ label }}</option></select></label><div class="flex gap-2"><button class="primary-button" type="submit">Сохранить</button><button class="secondary-button" type="button" @click="editing = false">Отмена</button></div>
      </fieldset></form>
      <template v-else><h2 class="font-semibold text-[15px]">{{ card.item.title || 'Без заголовка' }}</h2><p class="whitespace-pre-line text-[13px] my-3">{{ card.item.summary || 'Саммари ещё не подготовлено' }}</p><div class="flex flex-wrap gap-1 mb-3"><span v-for="tag in card.item.tags" :key="tag" class="tag">{{ tag }}</span></div><button class="outline-button" :disabled="busy" @click="edit">Редактировать материал</button></template>
      <section class="detail-section"><h3>Оценка и ключевые сущности</h3><p>{{ card.item.reasoning || 'Обоснование ещё не подготовлено' }}</p><p class="text-muted mt-1">Приоритет: {{ PRIORITIES[card.item.priority] }} · Уверенность: {{ card.item.confidence == null ? '—' : Math.round(card.item.confidence * 100) + '%' }}</p><p v-if="card.item.degraded || card.item.needs_review" class="text-muted">Результат требует проверки аналитиком.</p><dl class="mt-2"><div v-for="entity in card.entities" :key="`${entity.role}:${entity.value}`" class="flex gap-2"><dt class="text-muted">{{ ENTITY_ROLES[entity.role] || entity.role }}:</dt><dd>{{ entity.value }}</dd></div></dl></section>
      <section class="detail-section"><h3>Публикации в группе · {{ card.sources.length }}</h3><p v-if="!card.sources.length" class="text-muted">Связанных публикаций нет</p><div v-for="source in card.sources" :key="source.id" class="py-1"><a v-if="safeUrl(source.url)" class="text-link" :href="safeUrl(source.url)" target="_blank" rel="noopener noreferrer">{{ source.title || source.url }}</a><span v-else>{{ source.title || 'Ссылка недоступна' }}</span><span class="text-muted"> · {{ source.source_name || 'Источник не указан' }}{{ source.is_canonical ? ' · первоисточник' : '' }}</span></div></section>
      <section class="detail-section"><h3>{{ card.item.type === 'npa' ? 'Ход рассмотрения НПА' : 'События материала' }}</h3><p v-if="card.item.type === 'npa'">Текущий статус: {{ card.item.npa_status || 'Не указан' }}</p><p v-if="!card.events.length" class="text-muted">История событий пока пуста</p><ol><li v-for="event in card.events" :key="event.id ?? event.created_at" class="py-2 border-b"><strong>{{ event.status }}</strong> · {{ formatDate(event.occurred_at || event.created_at) }}<p>{{ event.note }}</p><a v-if="safeUrl(event.source_url)" class="text-link" :href="safeUrl(event.source_url)" target="_blank" rel="noopener noreferrer">Документ события</a></li></ol><button class="secondary-button" :disabled="busy" @click="eventOpen = !eventOpen">Добавить событие / срок</button>
      <form v-if="eventOpen" class="space-y-3 mt-3" @submit.prevent="addEvent"><fieldset :disabled="busy" class="space-y-3">
        <label class="field-label">Статус или событие<input v-model="eventDraft.status" required maxlength="64" list="event-statuses" class="form-control" placeholder="Слушания, срок или статус НПА" /><datalist id="event-statuses"><option v-for="status in NPA_STATUSES" :key="status" :value="status" /></datalist></label>
        <label class="field-label">Дата события<input v-model="eventDraft.date" type="datetime-local" class="form-control" /></label>
        <label class="field-label">Ссылка события<input v-model="eventDraft.source_url" class="form-control" /></label>
        <label class="field-label">Описание события<textarea v-model="eventDraft.note" class="form-control" /></label>
        <p class="text-muted text-[11px]">Событие сохраняется в истории. Статус НПА продвигается вперёд, если он не закреплён правкой аналитика. Сроки не создают напоминаний.</p>
        <button class="primary-button">Сохранить событие</button><button type="button" class="secondary-button ml-2" @click="eventOpen = false">Отменить событие</button>
      </fieldset></form></section>
      <section class="detail-section"><h3>Заметки аналитика</h3><p v-if="card.item.analyst_note" class="whitespace-pre-line">{{ card.item.analyst_note }}</p><div v-for="entry in card.notes" :key="entry.id ?? entry.created_at" class="py-2 border-b"><p class="whitespace-pre-line">{{ entry.body }}</p><small class="text-muted">{{ entry.author || 'Автор не указан' }} · {{ formatDate(entry.created_at, true) }}</small></div><form class="space-y-2 mt-2" @submit.prevent="note.trim() && perform(() => api.note(id, note.trim(), author.trim()), 'Заметка сохранена.', () => { note = '' })"><label class="field-label">Новая заметка<textarea v-model="note" required rows="2" class="form-control" /></label><label class="field-label">Автор (необязательно)<input v-model="author" class="form-control" /></label><button class="primary-button" :disabled="busy || !note.trim()">Добавить заметку</button></form></section>
      <section class="detail-section"><div class="flex items-center justify-between"><h3>История правок</h3><button class="text-link" :disabled="busy" @click="revisions">Обновить историю</button></div><p v-if="!card.revisions.length" class="text-muted">Правок пока нет</p><div v-for="entry in card.revisions" :key="entry.id ?? `${entry.field}:${entry.created_at}`" class="py-2 border-b"><strong>{{ entry.field }}</strong> · {{ entry.actor }} · {{ formatDate(entry.created_at, true) }}<p class="text-muted whitespace-pre-wrap">{{ entry.old_value || '—' }} → {{ entry.new_value || '—' }}</p><small>{{ EDIT_REASONS[entry.edit_reason] || entry.edit_reason }}</small></div><div v-for="(proposal, field) in card.model_proposals" :key="field"><div v-if="proposal" class="py-2"><p>Версия модели · {{ field }}: {{ proposal.new_value }}</p><button class="text-link" :disabled="busy" @click="perform(() => api.revert(id, String(field)), 'Версия модели восстановлена.')">Вернуть версию модели: {{ field }}</button></div></div></section>
      <section class="detail-section"><h3>Видимость материала</h3><p v-if="card.item.hidden_reason" class="text-muted">{{ card.item.hidden_reason }}</p><label class="field-label">Причина (необязательно)<input v-model="reason" class="form-control" /></label><div class="flex flex-wrap gap-2 mt-2"><button class="secondary-button" :disabled="busy" @click="perform(() => api.hideItem(id, 'feed', reason), 'Материал скрыт из ленты.')">Скрыть из ленты</button><button class="secondary-button" :disabled="busy" @click="perform(() => api.hideItem(id, 'digest', reason), 'Материал исключён из дайджеста.')">Исключить из дайджеста</button><button v-if="card.item.visibility === 'hidden_feed' || card.item.visibility === 'hidden_digest'" class="secondary-button" :disabled="busy" @click="perform(() => api.unhideItem(id), 'Материал возвращён в ленту.')">Вернуть в ленту</button><button v-if="card.item.visibility !== 'deleted'" class="secondary-button" :disabled="busy" @click="perform(() => api.deleteItem(id), 'Материал удалён. Его можно восстановить.')">Удалить материал</button><button v-else class="primary-button" :disabled="busy" @click="perform(() => api.restoreItem(id), 'Материал восстановлен.')">Восстановить материал</button></div><div class="mt-3"><p>{{ card.item.is_archived ? 'Материал в архиве' : 'Материал не в архиве' }}</p><button v-if="card.item.is_archived" class="outline-button" :disabled="busy" @click="perform(() => api.unarchive(id), 'Материал возвращён из архива.')">Вернуть из архива</button><button v-else class="outline-button" :disabled="busy" @click="perform(() => api.archive(id), 'Материал перемещён в архив.')">В архив</button><p class="text-muted text-[11px]">Архивные материалы исключены из дайджеста. Найти их можно с фильтром «Архив» в ленте.</p></div></section>
    </template>
  </AppModal>
</template>
