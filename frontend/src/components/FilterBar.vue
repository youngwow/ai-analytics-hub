<script setup lang="ts">
import type { FeedQuery } from '../api/types'
import { useDashboard } from '../composables/dashboard'
import { CATEGORIES, NPA_STATUSES, PRIORITIES, TYPES } from '../data/dashboard'
const props = defineProps<{ modelValue: FeedQuery; npa?: boolean; documents?: boolean; archive?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [FeedQuery] }>()
const { filters } = useDashboard()
function set(key: keyof FeedQuery, value: unknown) {
  emit('update:modelValue', { ...props.modelValue, [key]: value, ...(key === 'type' && value !== 'npa' ? { npa_status: undefined } : {}), cursor: undefined })
}
const value = (event: Event) => (event.target as HTMLInputElement).value
</script>
<template>
  <div class="toolbar filter-toolbar gap-3 py-2.5" aria-label="Фильтры материалов">
    <label class="field-label">Источник<select class="form-control" :value="modelValue.source_id?.[0] ?? ''" @change="set('source_id', value($event) ? [Number(value($event))] : undefined)"><option value="">Все источники</option><option v-for="source in filters?.sources ?? []" :key="source.id" :value="source.id">{{ source.name }}</option></select></label>
    <label v-if="!documents && !npa" class="field-label">Тип<select class="form-control" :value="modelValue.type ?? ''" @change="set('type', value($event) || undefined)"><option value="">Все типы</option><option v-for="(label, key) in TYPES" :key="key" :value="key">{{ label }}</option></select></label>
    <label v-if="npa || modelValue.type === 'npa'" class="field-label">Статус НПА<select class="form-control" :value="modelValue.npa_status ?? ''" @change="set('npa_status', value($event) || undefined)"><option value="">Все статусы</option><option v-for="status in filters?.npa_statuses ?? NPA_STATUSES" :key="status">{{ status }}</option></select></label>
    <label v-if="!documents" class="field-label">Приоритет<select class="form-control" :value="modelValue.priority?.[0] ?? ''" @change="set('priority', value($event) ? [value($event)] : undefined)"><option value="">Все приоритеты</option><option v-for="(label, key) in PRIORITIES" :key="key" :value="key">{{ label }}</option></select></label>
    <label v-if="!documents" class="field-label">Категория / тег<select class="form-control" :value="modelValue.tag?.[0] ?? ''" @change="set('tag', value($event) ? [value($event)] : undefined)"><option value="">Все категории</option><option v-for="tag in [...new Set([...CATEGORIES, ...(filters?.tags ?? [])])]" :key="tag">{{ tag }}</option></select></label>
    <label v-if="archive" class="field-label">Архив<select class="form-control" :value="modelValue.archived ?? 'exclude'" @change="set('archived', value($event))"><option value="exclude">Без архивных</option><option value="only">Только архивные</option><option value="include">Включая архивные</option></select></label>
    <label class="field-label">С даты<input type="date" class="form-control" :value="modelValue.from ?? ''" @change="set('from', value($event) || undefined)" /></label>
    <label class="field-label">По дату<input type="date" class="form-control" :value="modelValue.to ?? ''" @change="set('to', value($event) || undefined)" /></label>
    <label v-if="!documents" class="field-label">Порядок<select class="form-control" :value="modelValue.order ?? 'published'" @change="set('order', value($event))"><option value="published">По публикации</option><option value="priority">По приоритету</option><option value="processed">По обработке</option></select></label>
    <button class="text-link self-end mb-2" @click="emit('update:modelValue', {})">Сбросить фильтры</button>
  </div>
</template>
