<script setup>
/** 轻量线性图标：避免引入额外图标依赖，统一 1.8px 描边。 */
defineProps({
  name: { type: String, default: 'grid' },
  size: { type: [Number, String], default: 18 }
})
</script>

<template>
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.8"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <g v-if="name === 'grid'">
      <rect x="4" y="4" width="6" height="6" rx="1.2" />
      <rect x="14" y="4" width="6" height="6" rx="1.2" />
      <rect x="4" y="14" width="6" height="6" rx="1.2" />
      <rect x="14" y="14" width="6" height="6" rx="1.2" />
    </g>
    <g v-else-if="name === 'wallet'">
      <path d="M4 7.5A2.5 2.5 0 0 1 6.5 5H18a2 2 0 0 1 2 2v11H6.5A2.5 2.5 0 0 1 4 15.5z" />
      <path d="M4 8h14.5a1.5 1.5 0 0 1 0 3H4" />
      <path d="M16 13.5h.01" />
    </g>
    <g v-else-if="name === 'users'">
      <circle cx="9" cy="8" r="3" />
      <path d="M3.5 19a5.5 5.5 0 0 1 11 0" />
      <path d="M16 5.5a3 3 0 0 1 0 5.8M17.5 14.2A5 5 0 0 1 21 19" />
    </g>
    <g v-else-if="name === 'chart'">
      <path d="M4 19V5M4 19h16" />
      <path d="m6.5 15 3.2-4.2 3.3 2.4 4.5-6.2" />
      <path d="M17.5 5H21v3.5" />
    </g>
    <g v-else-if="name === 'calendar'">
      <rect x="3.5" y="5.5" width="17" height="15" rx="2" />
      <path d="M7.5 3.5v4M16.5 3.5v4M3.5 10.5h17" />
      <path d="M8 14h.01M12 14h.01M16 14h.01M8 17.5h.01M12 17.5h.01M16 17.5h.01" />
    </g>
    <g v-else-if="name === 'report'">
      <path d="M6 3.5h8l4 4V20H6z" />
      <path d="M14 3.5V8h4M9 12h6M9 15.5h6" />
    </g>
    <g v-else-if="name === 'bell'">
      <path d="M6.5 10a5.5 5.5 0 0 1 11 0c0 5 2 6 2 6h-15s2-1 2-6" />
      <path d="M10 20h4" />
    </g>
    <g v-else-if="name === 'message'">
      <path d="M4 5.5h16v11H9l-5 3z" />
      <path d="M8 9.5h8M8 13h5" />
    </g>
    <g v-else-if="name === 'fire'">
      <path d="M12 3s5 4.2 5 9a5 5 0 0 1-10 0c0-2.2 1-4.2 2.5-5.7.3 1.7 1.2 2.8 2.5 3.2C12.6 7.4 12.3 5 12 3z" />
    </g>
    <g v-else-if="name === 'settings'">
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4v-.2a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.6-1H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3 1.7 1.7 0 0 0 1-1.6v-.2h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1z" />
    </g>
    <g v-else-if="name === 'sun'">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </g>
    <g v-else-if="name === 'moon'">
      <path d="M20.2 15.2A8.5 8.5 0 0 1 8.8 3.8 8.5 8.5 0 1 0 20.2 15.2z" />
    </g>
    <g v-else-if="name === 'search'">
      <circle cx="10.8" cy="10.8" r="6.2" />
      <path d="m16 16 4.2 4.2" />
    </g>
    <g v-else-if="name === 'refresh'">
      <path d="M20 7v5h-5M4 17v-5h5" />
      <path d="M6.3 8.5A7 7 0 0 1 19 10M17.7 15.5A7 7 0 0 1 5 14" />
    </g>
    <g v-else-if="name === 'menu'">
      <path d="M4 7h16M4 12h16M4 17h16" />
    </g>
    <g v-else-if="name === 'close'">
      <path d="m6 6 12 12M18 6 6 18" />
    </g>
    <g v-else-if="name === 'chevron'">
      <path d="m9 6 6 6-6 6" />
    </g>
    <g v-else-if="name === 'shield'">
      <path d="M12 3 20 6v5.5c0 4.8-3.2 7.8-8 9.5-4.8-1.7-8-4.7-8-9.5V6z" />
      <path d="m8.5 12 2.2 2.2 4.8-4.8" />
    </g>
    <g v-else>
      <circle cx="12" cy="12" r="8" />
    </g>
  </svg>
</template>
