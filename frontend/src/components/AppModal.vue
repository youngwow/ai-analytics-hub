<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
defineProps<{ title: string }>()
const emit = defineEmits<{ close: [] }>()
const dialog = ref<HTMLDialogElement>()
const previousFocus = document.activeElement as HTMLElement | null
onMounted(() => dialog.value?.showModal())
onUnmounted(() => previousFocus?.focus())
</script>

<template>
  <Teleport to="body">
    <dialog ref="dialog" class="app-modal" aria-labelledby="modal-title" @cancel.prevent="emit('close')" @click="event => { if (event.target === dialog) emit('close') }">
      <div class="modal-inner">
        <div class="flex items-center justify-between gap-3 mb-5">
          <h2 id="modal-title" class="text-[15px] font-semibold">{{ title }}</h2>
          <button class="icon-button" aria-label="Закрыть" @click="emit('close')">×</button>
        </div>
        <slot />
      </div>
    </dialog>
  </Teleport>
</template>
