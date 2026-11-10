<script setup>
/**
 * 采集进度：WeUI progress + GSAP 宽度过渡。
 * 只有读取、入库、Token 分析和额度评估全部结束才到 100%。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../api'
import Icon from './Icon.vue'
import { gsap, reduceMotion } from '../motion'

const status = ref(null)
const failed = ref(false)
const barEl = ref(null)
const checkBarEl = ref(null)
let pollTimer = null

const STEPS = [
  { label: '准备', stages: ['preparing', 'checking'] },
  { label: '读取聊天', stages: ['refreshing', 'reading', 'collected'] },
  { label: '写入数据库', stages: ['ingesting'] },
  { label: '分析 Token', stages: ['metering', 'evaluating', 'done'] }
]

const stageLabels = {
  idle: '待启动',
  preparing: '准备采集',
  refreshing: '读取联系人',
  reading: '读取聊天记录',
  collected: '整理采集结果',
  ingesting: '写入数据库',
  metering: '分析 Token',
  evaluating: '额度评估',
  checking: '增量检查',
  done: '采集与分析完成',
  error: '采集失败'
}

const progress = computed(() => status.value?.progress || {})
const stage = computed(() => progress.value.stage || 'idle')
const stageText = computed(() => stageLabels[stage.value] || '采集进行中')
const active = computed(() =>
  Boolean(progress.value.active) || status.value?.status === 'running'
)
const isDone = computed(() => stage.value === 'done' || stage.value === 'checking')
const isError = computed(() => stage.value === 'error')
const checking = computed(() => stage.value === 'checking' && Boolean(progress.value.check_active))
const checkPercent = computed(() => {
  const value = Number(progress.value.check_percent || 0)
  return Math.max(0, Math.min(100, value))
})
const checkDetail = computed(() => {
  const p = progress.value
  const pieces = []
  if (p.check_current) pieces.push(p.check_current)
  if (p.check_total) pieces.push(`${p.check_processed || 0} / ${p.check_total} 位联系人`)
  if (p.check_messages !== undefined && p.check_messages !== null) {
    pieces.push(`本轮 ${Number(p.check_messages).toLocaleString()} 条`)
  }
  return pieces.join(' · ') || '正在扫描联系人…'
})
const percent = computed(() => {
  if (isError.value) return Math.max(0, Math.min(100, Number(progress.value.percent || 0)))
  if (isDone.value) return 100
  return Math.max(0, Math.min(100, Number(progress.value.percent || 0)))
})
const badgeClass = computed(() => {
  if (isError.value) return 'weui-badge_danger'
  if (isDone.value || active.value) return ''
  return 'weui-badge_plain'
})
const stepIndex = computed(() => {
  if (isDone.value) return STEPS.length
  if (isError.value || stage.value === 'idle') {
    const value = Number(progress.value.percent || 0)
    if (value < 8) return 0
    if (value < 85) return 1
    if (value < 91) return 2
    return 3
  }
  const index = STEPS.findIndex((step) => step.stages.includes(stage.value))
  return index < 0 ? 0 : index
})

const detail = computed(() => {
  const p = progress.value
  if (isError.value) return p.message || '采集器异常，请查看日志'
  if (stage.value === 'idle') return '等待 wechat-cli 开始采集'
  if (stage.value === 'checking') {
    return '总采集已完成，正在做本轮增量检查…'
  }
  if (isDone.value) {
    const pieces = []
    if (p.inserted) pieces.push(`本轮新增 ${Number(p.inserted).toLocaleString()} 条`)
    if (p.messages) pieces.push(`读取 ${Number(p.messages).toLocaleString()} 条`)
    if (p.completed_at) {
      const at = new Date(p.completed_at)
      if (!Number.isNaN(at.getTime())) {
        pieces.push(`完成于 ${at.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}`)
      }
    }
    return pieces.join(' · ') || '所有聊天记录已采集并完成分析'
  }
  const pieces = []
  if (p.current) pieces.push(p.current)
  if (p.total) pieces.push(`${p.processed || 0} / ${p.total} 位联系人`)
  if (p.messages !== undefined && p.messages !== null) {
    pieces.push(`累计 ${Number(p.messages).toLocaleString()} 条消息`)
  }
  return pieces.join(' · ') || '正在准备…'
})

function stepClass(index) {
  if (isDone.value) return 'done'
  if (index < stepIndex.value) return 'done'
  if (index === stepIndex.value && active.value) return 'active'
  if (index === stepIndex.value && stage.value !== 'idle') return 'current'
  return 'pending'
}

async function loadStatus() {
  try {
    status.value = await api.syncStatus()
    failed.value = false
  } catch {
    failed.value = true
  }
}

watch(percent, async (value) => {
  await nextTick()
  if (!barEl.value) return
  if (reduceMotion()) {
    barEl.value.style.width = `${value}%`
    return
  }
  gsap.to(barEl.value, { width: `${value}%`, duration: 0.75, ease: 'power2.out' })
}, { immediate: true })

watch(checkPercent, async (value) => {
  await nextTick()
  if (!checkBarEl.value) return
  if (reduceMotion()) {
    checkBarEl.value.style.width = `${value}%`
    return
  }
  gsap.to(checkBarEl.value, { width: `${value}%`, duration: 0.6, ease: 'power2.out' })
})

onMounted(() => {
  loadStatus()
  pollTimer = window.setInterval(loadStatus, 1200)
})
onBeforeUnmount(() => {
  if (pollTimer) window.clearInterval(pollTimer)
})
</script>

<template>
  <section id="service" class="weui-panel collect-card" :class="{ done: isDone, error: isError }">
    <div class="weui-panel__hd collect-head">
      <div class="collect-title">
        <span class="collect-icon" :class="{ spin: active && !isDone }">
          <Icon name="refresh" :size="16" />
        </span>
        <span class="collect-copy">
          <strong>数据采集</strong>
          <small>wechat-cli → 本地数据库 → Token 分析</small>
        </span>
      </div>
      <div class="collect-meta">
        <span class="weui-badge" :class="badgeClass">{{ failed ? '连接失败' : stageText }}</span>
        <span class="collect-percent num">{{ percent }}<i>%</i></span>
      </div>
    </div>

    <div class="weui-panel__bd">
      <div class="collect-steps" aria-hidden="true">
        <div
          v-for="(step, index) in STEPS"
          :key="step.label"
          class="collect-step"
          :class="stepClass(index)"
        >
          <span class="step-dot">{{ stepClass(index) === 'done' ? '✓' : index + 1 }}</span>
          <span class="step-label">{{ step.label }}</span>
          <span v-if="index < STEPS.length - 1" class="step-line"></span>
        </div>
      </div>

      <div class="weui-progress collect-progress">
        <div class="weui-progress__bar">
          <div
            ref="barEl"
            class="weui-progress__inner-bar"
            :style="{ width: `${percent}%`, background: isError ? 'var(--danger)' : 'var(--accent)' }"
          ></div>
        </div>
      </div>

      <div v-if="checking" class="collect-check">
        <div class="collect-check-head">
          <span>本轮增量检查</span>
          <span class="num">{{ checkPercent }}%</span>
        </div>
        <div class="weui-progress collect-check-progress">
          <div class="weui-progress__bar">
            <div
              ref="checkBarEl"
              class="weui-progress__inner-bar"
              :style="{ width: `${checkPercent}%` }"
            ></div>
          </div>
        </div>
        <div class="collect-check-detail">{{ checkDetail }}</div>
      </div>

      <div class="collect-detail">
        <span class="collect-dot" :class="{ live: active && !isDone }"></span>
        <span>{{ failed ? '无法读取采集状态，大屏数据仍可正常查看' : detail }}</span>
      </div>
    </div>
  </section>
</template>

<style scoped>
.collect-card { margin-bottom: 18px; }
.collect-head { align-items: center; }
.collect-title { display: flex; align-items: center; gap: 10px; min-width: 0; }
.collect-icon {
  display: grid;
  place-items: center;
  width: 30px;
  height: 30px;
  flex: 0 0 30px;
  border: 1px solid color-mix(in srgb, var(--accent) 25%, var(--border));
  border-radius: 9px;
  background: var(--accent-soft);
  color: var(--accent-strong);
}
.collect-icon.spin svg { animation: collect-spin 2.4s linear infinite; }
@keyframes collect-spin { to { transform: rotate(360deg); } }
.collect-copy { display: flex; flex-direction: column; min-width: 0; line-height: 1.25; }
.collect-copy strong { color: var(--text); font-size: 13px; font-weight: 650; }
.collect-copy small {
  margin-top: 2px;
  overflow: hidden;
  color: var(--muted);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.collect-meta { display: flex; align-items: center; gap: 10px; flex: 0 0 auto; }
.collect-percent {
  min-width: 48px;
  color: var(--text);
  font-size: 19px;
  font-weight: 700;
  letter-spacing: -0.03em;
  text-align: right;
}
.collect-percent i { margin-left: 1px; color: var(--faint); font-size: 10px; font-style: normal; font-weight: 500; }
.collect-steps { display: flex; align-items: center; margin-bottom: 10px; }
.collect-step { display: flex; align-items: center; gap: 6px; min-width: 0; color: var(--faint); font-size: 10px; white-space: nowrap; }
.step-dot {
  display: grid;
  place-items: center;
  width: 16px;
  height: 16px;
  flex: 0 0 16px;
  border: 1px solid var(--border);
  border-radius: 50%;
  background: var(--surface-2);
  color: var(--faint);
  font-size: 8px;
  font-weight: 650;
}
.step-line { width: 24px; height: 1px; flex: 0 0 24px; margin: 0 7px; background: var(--border); }
.collect-step.done { color: var(--muted); }
.collect-step.done .step-dot { border-color: color-mix(in srgb, var(--accent) 42%, var(--border)); background: var(--accent-soft); color: var(--accent-strong); }
.collect-step.done .step-line { background: color-mix(in srgb, var(--accent) 48%, var(--border)); }
.collect-step.active { color: var(--text); font-weight: 600; }
.collect-step.active .step-dot { border-color: var(--accent); background: var(--accent); color: var(--accent-ink); box-shadow: 0 0 0 3px var(--accent-soft); }
.collect-step.current { color: var(--muted); }
.collect-progress { margin-top: 2px; }
.collect-check {
  margin-top: 10px;
  padding: 10px 12px;
  border: 1px dashed color-mix(in srgb, var(--accent) 38%, var(--border));
  border-radius: 10px;
  background: color-mix(in srgb, var(--accent) 5%, var(--surface));
}
.collect-check-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 6px;
  color: var(--muted);
  font-size: 10px;
}
.collect-check-head .num { color: var(--accent-strong); font-weight: 650; }
.collect-check-progress .weui-progress__bar { height: 4px; }
.collect-check-detail {
  margin-top: 6px;
  overflow: hidden;
  color: var(--faint);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.collect-detail { display: flex; align-items: center; gap: 7px; margin-top: 9px; min-width: 0; color: var(--muted); font-size: 10px; }
.collect-detail span:last-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.collect-dot { width: 6px; height: 6px; flex: 0 0 6px; border-radius: 50%; background: var(--border-strong); }
.collect-dot.live { background: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
.collect-card.done .collect-dot { background: var(--accent); }
.collect-card.error .collect-dot { background: var(--danger); }
@media (max-width: 720px) {
  .collect-steps { overflow-x: auto; padding-bottom: 2px; }
  .step-line { width: 12px; flex-basis: 12px; margin: 0 4px; }
}
</style>
