<script setup lang="ts">
withDefaults(defineProps<{ name: string; size?: number; filled?: boolean }>(), { size: 15, filled: false })
</script>

<template>
  <svg :width="size" :height="size" viewBox="0 0 15 15" fill="none" aria-hidden="true" class="shrink-0">
    <g v-if="name === 'feed'" fill="currentColor">
      <rect x="1" y="2" width="13" height="2" rx="1" opacity=".5" />
      <rect x="1" y="6.5" width="9" height="2" rx="1" />
      <rect x="1" y="11" width="11" height="2" rx="1" opacity=".5" />
    </g>
    <g v-else-if="name === 'npa'" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round">
      <path d="M3 1h7l3 3v10H3V1z M10 1v3h3" /><path d="M5 6h5M5 8.5h5M5 11h3" opacity=".6" />
    </g>
    <g v-else-if="name === 'digest'" stroke="currentColor" stroke-width="1.2">
      <rect x="1" y="1" width="13" height="13" rx="2" /><path d="M4 5h7M4 7.5h5M4 10h6" stroke-linecap="round" opacity=".7" />
    </g>
    <g v-else-if="name === 'sources'" stroke="currentColor" stroke-width="1.2">
      <circle cx="7.5" cy="7.5" r="2" /><circle cx="7.5" cy="7.5" r="6" opacity=".4" />
      <path d="M7.5 1.5v12M2 5.5c1.5.5 3.5.8 5.5.8s4-.3 5.5-.8M2 9.5c1.5-.5 3.5-.8 5.5-.8s4 .3 5.5.8" opacity=".3" />
    </g>
    <g v-else-if="name === 'logo'" stroke="currentColor" stroke-width="1.4">
      <circle cx="7.5" cy="7.5" r="3.5" /><circle cx="7.5" cy="7.5" r="1.3" fill="currentColor" stroke="none" />
    </g>
    <g v-else-if="name === 'search'" transform="scale(1.25)" stroke="currentColor" stroke-width="1.2">
      <circle cx="5" cy="5" r="3.5" /><path d="m7.5 7.5 3 3" stroke-linecap="round" />
    </g>
    <g v-else-if="name === 'sun'" stroke="currentColor" stroke-width="1.2" transform="scale(1.07)">
      <circle cx="7" cy="7" r="2.5" /><path d="M7 1v1.5M7 11.5V13M1 7h1.5M11.5 7H13" stroke-linecap="round" />
    </g>
    <path v-else-if="name === 'moon'" d="M12 8.5A5 5 0 0 1 5.5 2a5 5 0 1 0 6.5 6.5z" stroke="currentColor" stroke-width="1.2" />
    <g v-else-if="name === 'bell'" stroke="currentColor" stroke-width="1.2" transform="scale(1.07)">
      <path d="M7 1.5A3.5 3.5 0 0 0 3.5 5v3L2.5 9.5h9l-1-1.5V5A3.5 3.5 0 0 0 7 1.5z M5.5 10.5a1.5 1.5 0 0 0 3 0" />
    </g>
    <path v-else-if="name === 'pencil'" d="M8.5 1.5l2 2-7 7H1.5v-2l7-7z" transform="scale(1.25)" stroke="currentColor" stroke-width="1.1" stroke-linejoin="round" />
    <path v-else-if="name === 'bookmark'" d="M2.5 1.5h8v10l-4-2.5-4 2.5V1.5z" transform="scale(1.15)" stroke="currentColor" stroke-width="1.1" :fill="filled ? 'currentColor' : 'none'" stroke-linejoin="round" />
    <g v-else-if="name === 'download'" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" transform="scale(1.15)">
      <path d="M6.5 1v8M3.5 6.5l3 3 3-3M1.5 10.5h10" />
    </g>
    <g v-else-if="name === 'refresh'" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" transform="scale(1.25)">
      <path d="M10 6A4 4 0 1 1 6 2M6 1l2 1-2 1" />
    </g>
    <path v-else-if="name === 'plus'" d="M6 2v8M2 6h8" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" transform="scale(1.25)" />
  </svg>
</template>
