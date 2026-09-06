<script setup lang="ts">
import { computed } from 'vue'
import MarkdownIt from 'markdown-it'
const props = defineProps<{ content: string }>()
// Raw HTML stays text; markdown-it also rejects unsafe link protocols.
const markdown = new MarkdownIt({ html: false, linkify: true })
const rendered = computed(() => markdown.render(props.content))
</script>
<template><article class="markdown-preview" aria-label="Предпросмотр дайджеста" v-html="rendered" /></template>
<style scoped>
.markdown-preview { font-size: 13px; line-height: 1.75; overflow-wrap: anywhere; }
.markdown-preview :deep(h1), .markdown-preview :deep(h2), .markdown-preview :deep(h3) { font-weight: 600; line-height: 1.35; margin: 1.2em 0 .6em; }
.markdown-preview :deep(h1) { font-size: 1.8em; }
.markdown-preview :deep(h2) { font-size: 1.45em; }
.markdown-preview :deep(h3) { font-size: 1.2em; }
.markdown-preview :deep(p), .markdown-preview :deep(ul), .markdown-preview :deep(ol) { margin: .75em 0; }
.markdown-preview :deep(ul) { list-style: disc; padding-left: 1.6em; }
.markdown-preview :deep(ol) { list-style: decimal; padding-left: 1.6em; }
.markdown-preview :deep(a) { color: var(--primary); text-decoration: underline; }
.markdown-preview :deep(blockquote) { border-left: 3px solid var(--border); padding-left: 1em; color: var(--muted-foreground); }
.markdown-preview :deep(pre) { padding: 1em; overflow-x: auto; border: 1px solid var(--border); border-radius: 6px; }
.markdown-preview :deep(code) { font-family: monospace; }
.markdown-preview :deep(table) { display: block; max-width: 100%; overflow-x: auto; border-collapse: collapse; }
.markdown-preview :deep(th), .markdown-preview :deep(td) { border: 1px solid var(--border); padding: .5em .8em; text-align: left; }
.markdown-preview :deep(hr) { border: 0; border-top: 1px solid var(--border); margin: 1.5em 0; }
.markdown-preview :deep(img) { max-width: 100%; }
</style>
