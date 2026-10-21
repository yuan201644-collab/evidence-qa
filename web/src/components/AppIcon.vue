<script setup lang="ts">
withDefaults(defineProps<{ name: string; size?: number }>(), { size: 20 })
const paths: Record<string, string[]> = {
  spark: ['m12 3 2.4 6.6L21 12l-6.6 2.4L12 21l-2.4-6.6L3 12l6.6-2.4L12 3Z'],
  chat: ['M21 11.5a8.5 8.5 0 0 1-8.5 8.5H4l-2 2V11.5A8.5 8.5 0 0 1 10.5 3h2a8.5 8.5 0 0 1 8.5 8.5Z', 'M7 10h10M7 14h6'],
  plus: ['M12 5v14M5 12h14'],
  arrow: ['M5 12h14m-6-6 6 6-6 6'],
  document: ['M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6Z', 'M14 2v6h6M8 12h8M8 16h5'],
  edit: ['M12 20h9', 'M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z'],
  trash: ['M3 6h18', 'M8 6V4h8v2', 'M19 6l-1 14H6L5 6', 'M10 11v5M14 11v5'],
  'list-check': ['M9 6h11M9 12h11M9 18h11', 'm3 6 1.5 1.5L7 5', 'm3 12 1.5 1.5L7 11', 'm3 18 1.5 1.5L7 17'],
  database: ['M20 5c0 1.7-3.6 3-8 3S4 6.7 4 5s3.6-3 8-3 8 1.3 8 3Z', 'M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5', 'M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3'],
  sources: ['M4 4h6v6H4zM14 14h6v6h-6z', 'M14 4h6v6h-6zM4 14h6v6H4z'],
  'cross-source': ['M3 4h6v6H3zM3 14h6v6H3z', 'M9 7h3l3 5-3 5H9', 'M15 9h6v6h-6z'],
  shield: ['m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z', 'm8 12 3 3 5-6'],
  link: ['M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-2 2', 'M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l2-2'],
  close: ['m6 6 12 12M6 18 18 6'],
  menu: ['M4 6h16M4 12h16M4 18h16'],
  more: ['M5 12h.01M12 12h.01M19 12h.01'],
  clock: ['M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z', 'M12 7v5l3 2'],
  chevron: ['m9 5 7 7-7 7'],
  help: ['M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z', 'M9 9a3 3 0 0 1 6 0c0 2-3 2-3 4M12 17h.01'],
  retry: ['M20 7v5h-5', 'M20 12a8 8 0 1 0-2.3 5.7M20 7l-2.3-2.7'],
  warning: ['m12 3 10 18H2L12 3Z', 'M12 9v5M12 17h.01'],
  pin: ['M9 3h6l-1 6 4 4H6l4-4-1-6ZM12 13v8'],
  panel: ['M3 4h18v16H3zM14 4v16'],
  'sidebar-fold': ['M4 4h16v16H4z', 'M9 4v16', 'm16 9-3 3 3 3'],
  stop: ['M6 6h12v12H6z'],
  copy: ['M8 8h12v13H8zM16 8V3H3v13h5'],
  sun: ['M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10Z', 'M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4'],
  moon: ['M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5Z'],
  chart: ['M4 20V10M10 20V4M16 20v-7M22 20H2'],
  check: ['m4 12 5 5L20 6'],
  layers: ['m12 3 9 5-9 5-9-5 9-5Z', 'm3 13 9 5 9-5', 'm3 17 9 5 9-5'],
  gear: ['M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z', 'M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2v.3a2 2 0 1 1-4 0v-.2a1.7 1.7 0 0 0-2.9-1.2l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.7 1.7 0 0 0 3.6 15h-.3a2 2 0 1 1 0-4h.2a1.7 1.7 0 0 0 1.2-2.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.7 1.7 0 0 0 10.4 3.6v-.3a2 2 0 1 1 4 0v.2a1.7 1.7 0 0 0 2.9 1.2l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0 1.2 2.9h.3a2 2 0 1 1 0 4h-.2a1.7 1.7 0 0 0-1.6 1.1Z'],
}
</script>

<template>
  <svg :width="size" :height="size" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" class="app-icon">
    <path v-for="(path, index) in paths[name] || paths.spark" :key="index" :d="path" />
  </svg>
</template>

<style scoped>
.app-icon { display: inline-block; flex-shrink: 0; vertical-align: middle; }
</style>
