<script setup>
/** 周报 / 月报：WeUI panel + 每日柱状图 + 区间汇总 + CSV 导出。 */
import { computed, ref, watch } from 'vue'
import EChart from './EChart.vue'
import { api } from '../api'
import { chartColors } from '../theme'

const props = defineProps({ peerId: { type: String, default: '' } })
const period = ref('week')
const data = ref({ items: [], tokens: 0, messages: 0, ranking: [] })
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    data.value = await api.report(period.value, props.peerId)
  } finally {
    loading.value = false
  }
}
watch([period, () => props.peerId], load, { immediate: true })
defineExpose({ load })

const option = computed(() => {
  const c = chartColors.value
  return ({
    grid: { left: 56, right: 20, top: 16, bottom: 28 },
    tooltip: {
      trigger: 'axis',
      backgroundColor: c.tooltip,
      borderColor: c.border,
      textStyle: { color: c.text },
      formatter: (ps) => {
        const p = ps[0]
        const it = data.value.items[p.dataIndex]
        return `${it.label}<br/><b>${it.tokens.toLocaleString()}</b> token，${it.messages} 条`
      }
    },
    xAxis: {
      type: 'category',
      data: data.value.items.map((i) => i.label),
      axisLine: { lineStyle: { color: c.axis } },
      axisLabel: { color: c.muted, interval: period.value === 'month' ? 2 : 0 }
    },
    yAxis: {
      type: 'value',
      splitLine: { lineStyle: { color: c.grid } },
      axisLabel: { color: c.muted, formatter: (v) => (v >= 10000 ? `${v / 1000}k` : v) }
    },
    series: [
      {
        type: 'bar',
        barWidth: '52%',
        data: data.value.items.map((i) => i.tokens),
        itemStyle: { borderRadius: [3, 3, 0, 0], color: c.accent }
      }
    ]
  })
})

const avg = computed(() => {
  const n = data.value.items.length || 1
  return Math.round(data.value.tokens / n)
})

async function exportCsv() {
  const rows = [['日期', 'token', '消息数'], ...data.value.items.map((i) => [i.bucket_start, i.tokens, i.messages])]
  const csv = rows.map((r) => r.join(',')).join('\n')
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `wequota-${period.value}-report.csv`
  a.click()
  URL.revokeObjectURL(a.href)
}
</script>

<template>
  <div class="weui-panel report-panel">
    <div class="weui-panel__hd">
      <div>
        <strong>{{ period === 'week' ? '周度统计' : '月度统计' }}</strong>
        <span class="panel-sub">每日 Token 消耗与消息量</span>
      </div>
      <div class="panel-actions">
        <button
          class="weui-btn weui-btn_mini"
          :class="period === 'week' ? 'weui-btn_primary' : 'weui-btn_default'"
          @click="period = 'week'"
        >周</button>
        <button
          class="weui-btn weui-btn_mini"
          :class="period === 'month' ? 'weui-btn_primary' : 'weui-btn_default'"
          @click="period = 'month'"
        >月</button>
        <button class="weui-btn weui-btn_mini weui-btn_default" @click="exportCsv">导出 CSV</button>
      </div>
    </div>
    <div class="weui-panel__bd">
      <div class="report-summary">
        <div><span>区间总量</span><b class="num">{{ data.tokens.toLocaleString() }}</b></div>
        <div><span>日均</span><b class="num">{{ avg.toLocaleString() }}</b></div>
        <div><span>消息数</span><b class="num">{{ data.messages.toLocaleString() }}</b></div>
      </div>
      <div v-if="loading && !data.items.length" class="empty">加载中…</div>
      <EChart v-else :option="option" height="220px" />
    </div>
  </div>
</template>

<style scoped>
.report-panel { height: 100%; }
.report-summary {
  display: flex;
  gap: 26px;
  margin-bottom: 10px;
  padding: 11px 14px;
  border-radius: 10px;
  background: var(--surface-2);
  border: 1px solid var(--border);
}
.report-summary div { display: flex; flex-direction: column; }
.report-summary span { font-size: 10px; color: var(--muted); }
.report-summary b { font-size: 18px; color: var(--text); }
</style>
