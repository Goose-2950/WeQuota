<script setup>
/** 时段 Token 消耗：WeUI panel + ECharts 面积图，可切小时 / 天。 */
import { computed, ref, watch } from 'vue'
import EChart from './EChart.vue'
import { api } from '../api'
import { chartColors } from '../theme'

const props = defineProps({
  peerId: { type: String, default: '' },
  peerName: { type: String, default: '' }
})

const bucket = ref('hour')
const items = ref([])
const loading = ref(false)

const ranges = [
  { key: 'hour', label: '按小时', days: 1 },
  { key: 'day', label: '按天', days: 7 },
  { key: 'day30', label: '近 30 天', days: 30 }
]
const range = ref('hour')

async function load() {
  loading.value = true
  try {
    const r = ranges.find((x) => x.key === range.value)
    const data = await api.timeseries(r.key === 'day30' ? 'day' : r.key, r.days, props.peerId)
    bucket.value = data.bucket
    items.value = data.items || []
  } finally {
    loading.value = false
  }
}

watch([range, () => props.peerId], load, { immediate: true })
defineExpose({ load })

const option = computed(() => {
  const labels = items.value.map((i) => i.label)
  const values = items.value.map((i) => i.tokens)
  const c = chartColors.value
  return {
    grid: { left: 56, right: 24, top: 28, bottom: 32 },
    tooltip: {
      trigger: 'axis',
      backgroundColor: c.tooltip,
      borderColor: c.border,
      textStyle: { color: c.text },
      formatter: (ps) => {
        const p = ps[0]
        const it = items.value[p.dataIndex]
        return `${it.label}<br/><b>${it.tokens.toLocaleString()}</b> token，${it.messages} 条消息`
      }
    },
    xAxis: {
      type: 'category',
      data: labels,
      boundaryGap: false,
      axisLine: { lineStyle: { color: c.axis } },
      axisLabel: { color: c.muted, interval: Math.max(0, Math.floor(labels.length / 8)) }
    },
    yAxis: {
      type: 'value',
      splitLine: { lineStyle: { color: c.grid } },
      axisLabel: { color: c.muted, formatter: (v) => (v >= 10000 ? `${v / 1000}k` : v) }
    },
    series: [
      {
        type: 'line',
        smooth: true,
        showSymbol: false,
        data: values,
        lineStyle: { color: c.accent, width: 2.5 },
        itemStyle: { color: c.accent },
        areaStyle: {
          color: {
            type: 'linear',
            x: 0,
            y: 0,
            x2: 0,
            y2: 1,
            colorStops: [
              { offset: 0, color: c.accentSoft },
              { offset: 1, color: 'transparent' }
            ]
          }
        }
      }
    ]
  }
})

const peak = computed(() =>
  items.value.reduce((m, i) => (i.tokens > m.tokens ? i : m), { tokens: 0, label: '-' })
)
</script>

<template>
  <div class="weui-panel chart-panel">
    <div class="weui-panel__hd">
      <div>
        <strong>时段消耗</strong>
        <span class="panel-sub">按小时 / 天查看 Token 使用趋势</span>
        <span v-if="peerName" class="weui-badge weui-badge_plain peer-badge">{{ peerName }}</span>
      </div>
      <div class="panel-actions chart-ranges">
        <button
          v-for="r in ranges"
          :key="r.key"
          class="weui-btn weui-btn_mini"
          :class="range === r.key ? 'weui-btn_primary' : 'weui-btn_default'"
          @click="range = r.key"
        >
          {{ r.label }}
        </button>
      </div>
    </div>
    <div class="weui-panel__bd">
      <div v-if="loading && !items.length" class="empty">加载中…</div>
      <div v-else-if="!items.length" class="empty">暂无数据</div>
      <template v-else>
        <div class="chart-note">
          峰值时段 <b class="num">{{ peak.label }}</b>，<span class="num">{{ peak.tokens.toLocaleString() }}</span> token
        </div>
        <EChart :option="option" height="330px" />
      </template>
    </div>
  </div>
</template>

<style scoped>
.chart-panel { height: 100%; }
.peer-badge { display: inline-flex; margin-top: 7px; }
.chart-ranges { display: flex; flex-wrap: wrap; }
.chart-note { color: var(--muted); font-size: 12px; margin: 2px 0 8px; }
.chart-note b { color: var(--text); font-weight: 650; }
</style>
