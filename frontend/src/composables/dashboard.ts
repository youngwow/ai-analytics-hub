import { computed, inject, ref, watch, type InjectionKey } from 'vue'
import { api } from '../api/client'
import type { Filters, Health, Status } from '../api/types'
export function createDashboard() {
  // Theme is the only persisted value. Old demo records are never read.
  const dark = ref(false)
  try { dark.value = window.localStorage.getItem('analytics-hub:theme') === 'dark' } catch { /* preference is optional */ }
  watch(dark, value => { try { window.localStorage.setItem('analytics-hub:theme', value ? 'dark' : 'light') } catch { /* preference is optional */ } })
  const search = ref('')
  const filters = ref<Filters | null>(null)
  const status = ref<Status | null>(null)
  const health = ref<Health | null>(null)
  const error = ref('')
  const loading = ref(false)
  const revision = ref(0)
  let generation = 0
  async function refresh() {
    const current = ++generation
    loading.value = true
    const results = await Promise.allSettled([api.filters(), api.status(), api.ready()])
    if (generation !== current) return
    filters.value = results[0].status === 'fulfilled' ? results[0].value : null
    status.value = results[1].status === 'fulfilled' ? results[1].value : null
    health.value = results[2].status === 'fulfilled' ? results[2].value : null
    const failures = results.filter(result => result.status === 'rejected').map(result => (result as PromiseRejectedResult).reason.message)
    if (health.value?.status === 'degraded') failures.push('Сервер отвечает, но не готов к работе. Подробности — в разделе «Сбор и обработка».')
    error.value = [...new Set(failures)].join(' · ')
    loading.value = false
  }
  function changed() { revision.value++; void refresh() }
  return { dark, search, filters, status, health, error, loading, revision, refresh, changed,
    activeSources: computed(() => status.value?.sources.active ?? 0) }
}
export const dashboardKey: InjectionKey<ReturnType<typeof createDashboard>> = Symbol('dashboard')
export function useDashboard() {
  const value = inject(dashboardKey)
  if (!value) throw new Error('Dashboard provider is missing')
  return value
}
