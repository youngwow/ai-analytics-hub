import { onScopeDispose, ref, shallowRef } from 'vue'
export function useRemote<T>(initial: T) {
  const data = shallowRef<T>(initial)
  const loading = ref(false)
  const error = ref('')
  let generation = 0
  let controller: AbortController | undefined
  async function run(load: (signal: AbortSignal) => Promise<T>) {
    controller?.abort()
    controller = new AbortController()
    const current = ++generation
    loading.value = true; error.value = ''
    try {
      const result = await load(controller.signal)
      if (current === generation) data.value = result
    } catch (reason) {
      if (current === generation && !(reason instanceof DOMException && reason.name === 'AbortError')) error.value = reason instanceof Error ? reason.message : 'Не удалось загрузить данные.'
    } finally { if (current === generation) loading.value = false }
  }
  onScopeDispose(() => { generation++; controller?.abort() })
  return { data, loading, error, run }
}
