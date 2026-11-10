<script setup>
/** 消耗构成：WeUI 列表 + 分段横条，展示消息类型的 Token 占比。 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { chartColors } from '../theme'
import { gsap, reduceMotion } from '../motion'

const props = defineProps({ items: { type: Array, default: () => [] } })
const panelEl = ref(null)

const LABELS = {
  text: '文字',
  voice: '语音消息',
  image: '图片',
  emoji: '表情',
  video: '视频消息',
  voice_call: '语音通话',
  video_call: '视频通话',
  file: '文件'
}

const total = computed(() => props.items.reduce((sum, item) => sum + Number(item.tokens || 0), 0))
const rows = computed(() => {
  const palette = chartColors.value.pie
  return props.items
    .filter((item) => Number(item.tokens || 0) > 0)
    .sort((a, b) => b.tokens - a.tokens)
    .map((item, index) => ({
      key: item.msg_type,
      label: LABELS[item.msg_type] || item.msg_type,
      tokens: Number(item.tokens || 0),
      percent: total.value ? (Number(item.tokens || 0) / total.value) * 100 : 0,
      color: palette[index % palette.length]
    }))
})

function animateSegments() {
  if (!panelEl.value || reduceMotion()) return
  const segments = panelEl.value.querySelectorAll('.type-segment')
  if (!segments.length) return
  gsap.fromTo(segments, { scaleX: 0 }, {
    scaleX: 1,
    duration: 0.7,
    stagger: 0.05,
    ease: 'power2.out',
    overwrite: true
  })
}

onMounted(animateSegments)
watch(rows, async () => {
  await nextTick()
  animateSegments()
})
</script>

<template>
  <div ref="panelEl" class="weui-panel type-panel">
    <div class="weui-panel__hd">
      <div>
        <strong>消耗构成</strong>
        <span class="panel-sub">按消息类型的 Token 占比</span>
      </div>
      <span class="weui-badge weui-badge_plain">按类型</span>
    </div>
    <div class="weui-panel__bd">
      <div v-if="!rows.length" class="empty">暂无数据</div>
      <template v-else>
        <div class="type-total">
          <b class="num">{{ total.toLocaleString() }}</b>
          <span>token</span>
        </div>
        <div class="type-stack" role="img" aria-label="消息类型 Token 占比">
          <span
            v-for="row in rows"
            :key="row.key"
            class="type-segment"
            :style="{ width: `${row.percent}%`, background: row.color }"
            :title="`${row.label} ${row.percent.toFixed(1)}%`"
          ></span>
        </div>
        <div class="weui-cells type-legend">
          <div v-for="row in rows" :key="row.key" class="weui-cell">
            <span class="weui-cell__hd"><i :style="{ background: row.color }"></i></span>
            <span class="weui-cell__bd">{{ row.label }}</span>
            <span class="weui-cell__ft type-values">
              <b class="num">{{ row.percent.toFixed(1) }}%</b>
              <small class="num">{{ row.tokens.toLocaleString() }}</small>
            </span>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.type-panel { height: 100%; }
.type-total { display: flex; align-items: baseline; gap: 6px; margin: 2px 0 12px; }
.type-total b { color: var(--text); font-size: 25px; font-weight: 700; }
.type-total span { color: var(--muted); font-size: 11px; }
.type-stack { display: flex; width: 100%; height: 14px; overflow: hidden; border-radius: 4px; background: var(--surface-3); }
.type-segment { display: block; height: 100%; min-width: 2px; transform-origin: left center; }
.type-legend { margin-top: 14px; }
.type-legend .weui-cell { padding: 8px 0; }
.type-legend .weui-cell__hd { display: flex; align-items: center; margin-right: 8px; }
.type-legend i { width: 7px; height: 7px; border-radius: 2px; }
.type-values { display: flex; align-items: baseline; gap: 10px; }
.type-values b { color: var(--text); font-weight: 650; }
.type-values small { color: var(--faint); }
</style>
