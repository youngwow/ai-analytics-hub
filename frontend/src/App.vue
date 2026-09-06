<script setup lang="ts">
import { computed, onMounted, onUnmounted, provide, ref, watch } from 'vue'
import AppIcon from './components/AppIcon.vue'
import NewsFeed from './components/NewsFeed.vue'
import NpaTracker from './components/NpaTracker.vue'
import DigestView from './components/DigestView.vue'
import SourcesPanel from './components/SourcesPanel.vue'
import StatusView from './components/StatusView.vue'
import ProfilesView from './components/ProfilesView.vue'
import { createDashboard, dashboardKey } from './composables/dashboard'
import { formatDate } from './utils/dashboard'
type Section = 'feed' | 'npa' | 'digest' | 'sources' | 'status' | 'profiles'
const store = createDashboard(); provide(dashboardKey, store)
const { dark, search, health, status, error, loading, activeSources } = store
const nav: { id: Section; label: string; title: string; icon: string }[] = [
  { id: 'feed', label: 'Мониторинг', title: 'Мониторинг', icon: 'feed' },
  { id: 'npa', label: 'НПА', title: 'Реестр НПА', icon: 'npa' },
  { id: 'digest', label: 'Дайджест', title: 'Дайджест', icon: 'digest' },
  { id: 'sources', label: 'Источники', title: 'Источники данных', icon: 'sources' },
  { id: 'status', label: 'Сбор и обработка', title: 'Сбор и обработка', icon: 'refresh' },
  { id: 'profiles', label: 'Профиль компании', title: 'Профили компании', icon: 'sources' },
]
const views = { feed: NewsFeed, npa: NpaTracker, digest: DigestView, sources: SourcesPanel, status: StatusView, profiles: ProfilesView }
const currentHash = (): Section => nav.some(item => item.id === location.hash.slice(1)) ? location.hash.slice(1) as Section : 'feed'
const section = ref(currentHash()); const mobileNav = ref(false); const notifications = ref(false)
const title = computed(() => nav.find(item => item.id === section.value)!.title)
function navigate(id: Section) { section.value = id; location.hash = id; search.value = ''; mobileNav.value = false; notifications.value = false }
function syncHash() { const next = currentHash(); if (section.value !== next) { section.value = next; search.value = '' } }
function dismiss(event: KeyboardEvent) { if (event.key === 'Escape') { mobileNav.value = false; notifications.value = false } }
function focusContent() { document.getElementById('main-content')?.focus() }
watch(dark, value => document.documentElement.classList.toggle('dark', value), { immediate: true })
watch(title, value => { document.title = `${value} · ИИ-индустрия` }, { immediate: true })
onMounted(() => { void store.refresh(); window.addEventListener('hashchange', syncHash); window.addEventListener('keydown', dismiss) })
onUnmounted(() => { window.removeEventListener('hashchange', syncHash); window.removeEventListener('keydown', dismiss) })
</script>
<template>
  <div class="app-shell">
    <a href="#main-content" class="skip-link" @click.prevent="focusContent">Перейти к содержимому</a>
    <button v-if="mobileNav" class="sidebar-scrim" aria-label="Закрыть навигацию" @click="mobileNav = false" />
    <aside id="sidebar" class="sidebar" :class="{ 'is-open': mobileNav }">
      <div class="flex items-center gap-2.5 px-4 py-3.5 border-b"><div class="logo-mark"><AppIcon name="logo" /></div><div><div class="font-semibold text-[12px] leading-tight">Мониторинг</div><div class="text-[10px] text-muted">ИИ-индустрия · ЦА</div></div></div>
      <nav class="flex-1 py-2 px-2" aria-label="Основная навигация"><div class="section-label px-2 py-1.5">Разделы</div><a v-for="item in nav" :key="item.id" :href="`#${item.id}`" class="nav-item" :class="{ active: section === item.id }" :aria-current="section === item.id ? 'page' : undefined" @click.prevent="navigate(item.id)"><AppIcon :name="item.icon" /><span class="flex-1">{{ item.label }}</span><span v-if="item.id === 'status' && status?.unprocessed" class="nav-badge">{{ status.unprocessed }}</span></a></nav>
      <div class="px-4 py-3 border-t text-[11px] text-muted"><div class="flex items-center gap-1.5"><span class="status-dot" :style="{ background: health?.status === 'ok' ? '#22c55e' : '#ef4444' }" />{{ loading ? 'Подключение…' : health?.status === 'ok' ? 'Сервер подключён' : health?.status === 'degraded' ? 'Сервер не готов' : 'Нет подключения' }}</div><p v-if="status" class="mt-1">{{ activeSources }} активных источников</p><p class="text-[10px] mt-1">Последний сбор: {{ formatDate(status?.last_collect_at, true) }}</p></div>
      <div class="px-4 py-3 border-t text-[11px] text-muted">Рабочее пространство аналитика<br /><span class="text-[10px]">Вход в аккаунт — скоро</span></div>
    </aside>
    <div class="flex flex-col flex-1 min-w-0 min-h-0">
      <header class="topbar"><button class="icon-button menu-toggle" aria-label="Открыть навигацию" aria-controls="sidebar" :aria-expanded="mobileNav" @click="mobileNav = !mobileNav"><AppIcon name="feed" /></button><h1 class="header-title flex-1 font-semibold text-[14px]">{{ title }}</h1><label class="search-box"><AppIcon name="search" :size="12" /><input v-model="search" type="search" aria-label="Поиск в текущем разделе" placeholder="Поиск, # карточки, теги…" /></label><button class="icon-button" aria-label="Обновить данные" :disabled="loading" @click="store.changed()"><AppIcon name="refresh" :size="14" /></button><button class="icon-button" :aria-label="dark ? 'Светлая тема' : 'Тёмная тема'" :aria-pressed="dark" @click="dark = !dark"><AppIcon :name="dark ? 'sun' : 'moon'" :size="14" /></button><div class="relative"><button class="icon-button relative" aria-label="Уведомления" :aria-expanded="notifications" @click="notifications = !notifications"><AppIcon name="bell" :size="14" /><span v-if="status?.stale_sources.length" class="notification-dot" /></button><div v-if="notifications" id="notifications" class="notification-panel"><strong>Состояние источников</strong><p v-if="!status" class="text-muted mt-2">Данные недоступны</p><p v-else-if="!status.stale_sources.length" class="text-muted mt-2">Просроченных источников нет</p><button v-for="source in status?.stale_sources ?? []" :key="source.id" class="notification-item" @click="navigate('sources'); search = source.name">{{ source.name }} · задержка {{ source.overdue_minutes }} мин.<br />{{ source.last_error }}</button></div></div></header>
      <div v-if="error" class="storage-alert" role="alert"><span>{{ error }}</span><button class="text-link ml-auto" :disabled="loading" @click="store.refresh()">Повторить подключение</button></div>
      <main id="main-content" tabindex="-1" class="flex-1 min-h-0 overflow-hidden"><component :is="views[section]" /></main>
    </div>
  </div>
</template>
