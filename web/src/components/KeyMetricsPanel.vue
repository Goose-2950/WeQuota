<script setup>
/** 关键指标：WeUI progress 可视化额度覆盖率、使用率、超限与采集状态。 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { gsap, reduceMotion } from '../motion'

const props = defineProps({
  summary: { type: Object, default: () => ({}) },
  usage: { type: Array, default: () => [] },
  collector: { type: Object, default: null }
})

const panelEl = ref(null)
const rules = computed(() => props.usage.filter((item) => item.limit))
const ratios = computed(() => rules.value.map((item) => Math.min(Number(item.ratio ?? 0), 1)))
const coverage = computed(() => {
  const peers = Number(props.summary.peers || 0)
  return peers > 0 ? Math.min((rules.value.length / peers) * 100, 100) : 0
})
const avgUse = computed(() =>
  ratios.value.length ? (ratios.value.reduce((a, b) => a + b, 0) / ratios.value.length) * 100 : 0
)
const danger = computed(() => rules.value.filter((item) => Number(item.ratio ?? 0) >= 1).length)
const riskRatio = computed(() => (rules.value.length ? (danger.value / rules.value.length) * 100 : 0))
const worker = computed(() => props.collector?.worker || {})
const collectorScore = computed(() => {
  const state = worker.value.status
  if (state === 'running') return 100
  if (state === 'sleeping') return 55
  if (state === 'error') return 15
  return 0
})
const collectorLabel = computed(() => {
  const state = worker.value.status
  return { running: '运行中', sleeping: '休眠', error: '异常', idle: '待启动' }[state] || '未连接'
})

const metrics = computed(() => [
  { label: '额度覆盖率', value: `${coverage.value.toFixed(0)}%`, detail: `${rules.value.length} / ${props.summary.peers || 0} 位联系人`, progress: coverage.value, tone: 'accent' },
  { label: '平均额度使用', value: `${avgUse.value.toFixed(0)}%`, detail: '当前统计窗口', progress: avgUse.value, tone: 'accent' },
  { label: '超限规则', value: String(danger.value), detail: danger.value ? '需要关注' : '全部正常', progress: riskRatio.value, tone: danger.value ? 'danger' : 'accent' },
  { label: '采集可用性', value: `${collectorScore.value}%`, detail: `采集器 ${collectorLabel.value}`, progress: collectorScore.value, tone: collectorScore.value < 50 ? 'warn' : 'accent' }
])

function animateBars() {
  if (!panelEl.value || reduceMotion()) return
  const bars = panelEl.value.querySelectorAll('.metric-fill')
  if (!bars.length) return
  gsap.fromTo(bars, { scaleX: 0 }, {
    scaleX: 1,
    duration: 0.7,
    stagger: 0.06,
    ease: 'power2.out',
    overwrite: true
  })
}

onMounted(animateBars)
watch(metrics, async () => {
  await nextTick()
  animateBars()
})
</script>

<template>
  <div ref="panelEl" class="weui-panel key-panel">
    <div class="weui-panel__hd">
      <div>
        <strong>关键指标</strong>
        <span class="panel-sub">当前区间表现</span>
      </div>
      <span class="weui-badge weui-badge_plain">概览</span>
    </div>
    <div class="weui-panel__bd">
      <div class="metric-list">
        <div v-for="metric in metrics" :key="metric.label" class="metric">
          <div class="metric-head">
            <span>{{ metric.label }}</span>
            <b class="num">{{ metric.value }}</b>
          </div>
          <div class="weui-progress metric-progress">
            <div class="weui-progress__bar">
              <div
                class="weui-progress__inner-bar metric-fill"
                :style="{ width: `${Math.max(2, Math.min(metric.progress, 100))}%`, background: metric.tone === 'danger' ? 'var(--danger)' : metric.tone === 'warn' ? 'var(--warn)' : 'var(--accent)' }"
              ></div>
            </div>
          </div>
          <div class="metric-detail">{{ metric.detail }}</div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.key-panel { height: 100%; }
.metric-list { display: flex; flex-direction: column; gap: 18px; }
.metric-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; color: var(--muted); font-size: 12px; }
.metric-head b { color: var(--text); font-size: 15px; font-weight: 700; }
.metric-progress { margin: 8px 0 5px; }
.metric-fill { transform-origin: left center; }
.metric-detail { color: var(--faint); font-size: 11px; }
</style>
