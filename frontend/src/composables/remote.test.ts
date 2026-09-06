import { effectScope } from 'vue'
import { describe, expect, it } from 'vitest'
import { useRemote } from './remote'
const deferred = <T>() => { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done }); return { promise, resolve } }
describe('remote data lifecycle', () => {
  it('ignores a late response for an old query and cancels its request', async () => {
    const scope = effectScope(); const remote = scope.run(() => useRemote(''))!
    const first = deferred<string>(); let signal!: AbortSignal
    const old = remote.run(value => { signal = value; return first.promise })
    await remote.run(async () => 'new query'); expect(signal.aborted).toBe(true)
    first.resolve('old query'); await old; expect(remote.data.value).toBe('new query'); scope.stop()
  })
  it('aborts on disposal and does not update an unmounted screen', async () => {
    const scope = effectScope(); const remote = scope.run(() => useRemote(''))!
    const pending = deferred<string>(); let signal!: AbortSignal
    const task = remote.run(value => { signal = value; return pending.promise }); scope.stop()
    expect(signal.aborted).toBe(true); pending.resolve('late'); await task; expect(remote.data.value).toBe('')
  })
  it('shows errors and clears them on a successful retry', async () => {
    const scope = effectScope(); const remote = scope.run(() => useRemote(''))!
    await remote.run(async () => { throw new Error('offline') }); expect(remote.error.value).toBe('offline'); expect(remote.loading.value).toBe(false)
    await remote.run(async () => 'server data'); expect(remote.error.value).toBe(''); expect(remote.data.value).toBe('server data'); scope.stop()
  })
})
