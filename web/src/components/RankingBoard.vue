<script setup>
/** 联系人 Token 消耗排行：WeUI panel + 横向条形图。 */
import { computed } from 'vue'
import EChart from './EChart.vue'
import { chartColors } from '../theme'

const props = defineProps({
  items: { type: Array, default: () => [] }
})

const sorted = computed(() => [...props.items].sort((a, b) => a.tokens - b.tokens))
const height = computed(() => `${Math.max(200, sorted.value.length * 36 + 64)}px`)

const option = computed(() => {
  const rows = sorted.value
  const c = chartColors.value
  return {
    grid: { left: 112, right: 86, top: 8, bottom: 24 },
    tooltip: {
      trigger: 'item',
      backgroundColor: c.tooltip,
      borderColor: c.border,
      textStyle: { color: c.text },
      formatter: (p) => {
        const d = rows[p.dataIndex]
        const quota = d.limit
          ? `<br/>窗口额度 ${d.limit.toLocaleString()}，已用 ${d.used.toLocaleString()}（${(d.ratio * 100).toFixed(0)}%）`
          : '<br/>未设额度'
        return `<b>${d.peer_name || d.peer_id}</b><br/>${d.tokens.toLocaleString()} token，${d.messages} 条${quota}`
      }
    },
    xAxis: { type: 'value', splitLine: { lineStyle: { color: c.grid } }, axisLabel: { color: c.muted } },
    yAxis: {
      type: 'category',
      data: rows.map((r) => r.peer_name || r.peer_id),
      axisLine: { lineStyle: { color: c.axis } },
      axisTick: { show: false },
      axisLabel: { color: c.text, fontSize: 11 }
    },
    series: [
      {
        type: 'bar',
        barWidth: 14,
        data: rows.map((r) => ({
          value: r.tokens,
          itemStyle: {
            borderRadius: [0, 7, 7, 0],
            color: r.limit && r.ratio >= 1 ? c.danger : r.limit && r.ratio >= 0.8 ? c.warn : c.accent
          }
        })),
        label: {
          show: true,
          position: 'right',
          color: c.muted,
          fontSize: 11,
          formatter: (p) => Number(p.value).toLocaleString()
        }
      }
    ]
  }
})
</script>

<template>
  <div class="weui-panel">
    <div class="weui-panel__hd">
      <div>
        <strong>联系人消耗排行</strong>
        <span class="panel-sub">按 Token 消耗从高到低排序</span>
      </div>
      <span class="weui-badge weui-badge_plain">TOP {{ items.length }}</span>
    </div>
    <div class="weui-panel__bd">
      <div v-if="!items.length" class="empty">暂无数据</div>
      <EChart v-else :option="option" :height="height" />
    </div>
  </div>
</template>

<style scoped>
.weui-panel { height: 100%; }
</style>
