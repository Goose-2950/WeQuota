<script setup>
/**
 * 年度消息频率热力图：WeUI panel + GitHub 贡献图式日历。
 * GSAP 在年份切换时让 365 个方块依次出现。
 */
import { computed, nextTick, ref, watch } from 'vue'
import { api } from '../api'
import { gsap, reduceMotion } from '../motion'

const props = defineProps({
  accountId: { type: String, default: '' }
})

const data = ref(null)
const loading = ref(false)
const error = ref('')
const year = ref(null)
const gridEl = ref(null)
let requestSeq = 0

const WEEKDAYS = ['一', '', '三', '', '五', '', '日']
const WEEKDAY_NAMES = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']

async function load(target = year.value) {
  const seq = ++requestSeq
  loading.value = true
  error.value = ''
  try {
    const payload = await api.heatmap(target || '')
    if (seq !== requestSeq) return
    data.value = payload
    year.value = payload.year
  } catch (e) {
    if (seq === requestSeq) error.value = String(e.message || e)
  } finally {
    if (seq === requestSeq) loading.value = false
  }
}

function pickYear(value) {
  if (value === year.value) return
  year.value = value
  load(value)
}

watch(
  () => props.accountId,
  () => {
    data.value = null
    year.value = null
    load('')
  },
  { immediate: true }
)

const items = computed(() => data.value?.items || [])
const years = computed(() => data.value?.years || [])
const total = computed(() => Number(data.value?.total || 0))
const activeDays = computed(() => Number(data.value?.active_days || 0))
const peak = computed(() => data.value?.peak || null)

const weeks = computed(() => {
  if (!items.value.length) return []
  const first = new Date(`${data.value.year}-01-01T00:00:00`)
  const offset = (first.getDay() + 6) % 7
  const cells = Array.from({ length: offset }, () => null).concat(items.value)
  while (cells.length % 7) cells.push(null)
  const result = []
  for (let index = 0; index < cells.length; index += 7) {
    result.push(cells.slice(index, index + 7))
  }
  return result
})

const gridStyle = computed(() => ({
  gridTemplateColumns: `repeat(${Math.max(weeks.value.length, 1)}, minmax(0, 1fr))`
}))

const months = computed(() => {
  const starts = new Map()
  weeks.value.forEach((week, weekIndex) => {
    week.forEach((day) => {
      if (!day) return
      const month = Number(day.date.slice(5, 7))
      if (!starts.has(month)) starts.set(month, weekIndex)
    })
  })
  const entries = [...starts.entries()].sort((a, b) => a[0] - b[0])
  return entries.map(([month, weekIndex], index) => {
    const next = entries[index + 1]?.[1] ?? weeks.value.length
    return {
      key: month,
      label: `${month}月`,
      week: weekIndex + 1,
      span: Math.max(1, next - weekIndex)
    }
  })
})

function cellTitle(day) {
  if (!day) return ''
  const date = new Date(`${day.date}T00:00:00`)
  const weekday = WEEKDAY_NAMES[date.getDay()]
  return `${day.date} ${weekday} · ${Number(day.count).toLocaleString()} 条消息${day.future ? '（未来）' : ''}`
}

function cellLabel(day) {
  return day ? `${day.date}，${day.count} 条消息` : ''
}

async function animateCells() {
  await nextTick()
  if (!gridEl.value) return
  const cells = gridEl.value.querySelectorAll('.heatmap-cell:not(.blank)')
  if (reduceMotion()) return
  gsap.fromTo(cells, { scale: 0.4, autoAlpha: 0 }, {
    scale: 1,
    autoAlpha: 1,
    duration: 0.3,
    stagger: { each: 0.001, from: 'start' },
    ease: 'power1.out',
    overwrite: true
  })
}

watch(items, animateCells)
</script>

<template>
  <div class="weui-panel heatmap-panel">
    <div class="weui-panel__hd">
      <div>
        <strong>消息频率</strong>
        <span class="panel-sub">当前账号每天的消息量 · 一格一天，按年查看</span>
      </div>
      <div class="panel-actions heatmap-years" role="group" aria-label="选择年份">
        <button
          v-for="item in years"
          :key="item"
          class="weui-btn weui-btn_mini"
          :class="item === year ? 'weui-btn_primary' : 'weui-btn_default'"
          @click="pickYear(item)"
        >
          {{ item }} 年
        </button>
      </div>
    </div>

    <div class="weui-panel__bd">
      <div class="heatmap-summary">
        <span class="heatmap-stat">
          <strong class="num">{{ total.toLocaleString() }}</strong>
          <small>全年消息</small>
        </span>
        <span class="heatmap-stat">
          <strong class="num">{{ activeDays.toLocaleString() }}</strong>
          <small>活跃天数</small>
        </span>
        <span v-if="peak" class="heatmap-stat">
          <strong class="num">{{ peak.count.toLocaleString() }}</strong>
          <small>单日峰值 · {{ peak.date.slice(5) }}</small>
        </span>
        <span class="heatmap-spacer"></span>
        <span class="heatmap-legend">
          <span>少</span>
          <i v-for="level in 5" :key="level" class="heatmap-swatch" :class="`level-${level - 1}`"></i>
          <span>多</span>
        </span>
      </div>

      <div v-if="error" class="empty">{{ error }}</div>
      <div v-else-if="loading && !data" class="empty">加载年度热力图…</div>
      <div v-else-if="!weeks.length" class="empty">暂无消息记录</div>
      <div v-else class="heatmap-scroll">
        <div class="heatmap-canvas">
          <div class="heatmap-months" :style="gridStyle" aria-hidden="true">
            <span
              v-for="month in months"
              :key="month.key"
              :style="{ gridColumn: `${month.week} / span ${month.span}` }"
            >
              {{ month.label }}
            </span>
          </div>
          <div class="heatmap-body">
            <div class="heatmap-weekdays" aria-hidden="true">
              <span v-for="(label, index) in WEEKDAYS" :key="index">{{ label }}</span>
            </div>
            <div
              ref="gridEl"
              class="heatmap-weeks"
              :style="gridStyle"
              role="img"
              :aria-label="`${year} 年每日消息频率热力图`"
            >
              <div v-for="(week, weekIndex) in weeks" :key="weekIndex" class="heatmap-week">
                <span
                  v-for="(day, dayIndex) in week"
                  :key="day ? day.date : `blank-${weekIndex}-${dayIndex}`"
                  class="heatmap-cell"
                  :class="[day ? `level-${day.level}` : 'blank', { future: day?.future }]"
                  :title="cellTitle(day)"
                  :aria-label="cellLabel(day)"
                ></span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.heatmap-panel { height: 100%; }
.heatmap-years { display: flex; flex-wrap: wrap; }
.heatmap-summary {
  display: flex;
  align-items: center;
  gap: 22px;
  flex-wrap: wrap;
  margin-bottom: 16px;
  padding: 11px 14px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--surface-2);
}
.heatmap-stat { display: inline-flex; align-items: baseline; gap: 7px; white-space: nowrap; }
.heatmap-stat strong { color: var(--text); font-size: 19px; font-weight: 700; letter-spacing: -0.02em; }
.heatmap-stat small { color: var(--muted); font-size: 10px; }
.heatmap-spacer { flex: 1 1 auto; }
.heatmap-legend { display: inline-flex; align-items: center; gap: 5px; color: var(--faint); font-size: 10px; }
.heatmap-swatch { display: inline-block; width: 10px; height: 10px; border: 1px solid transparent; border-radius: 3px; background: var(--surface-3); }
.heatmap-swatch.level-1 { background: color-mix(in srgb, var(--accent) 22%, var(--surface)); }
.heatmap-swatch.level-2 { background: color-mix(in srgb, var(--accent) 42%, var(--surface)); }
.heatmap-swatch.level-3 { background: color-mix(in srgb, var(--accent) 68%, var(--surface)); }
.heatmap-swatch.level-4 { background: var(--accent-strong); }
.heatmap-scroll { overflow-x: auto; padding-bottom: 2px; }
.heatmap-canvas { min-width: 760px; }
.heatmap-months { display: grid; gap: 4px; height: 16px; margin-left: 28px; margin-bottom: 5px; }
.heatmap-months span { overflow: hidden; color: var(--muted); font-size: 10px; line-height: 1; white-space: nowrap; }
.heatmap-body { display: flex; gap: 8px; }
.heatmap-weekdays { display: grid; grid-template-rows: repeat(7, 1fr); gap: 4px; width: 20px; color: var(--faint); font-size: 9px; line-height: 1; }
.heatmap-weekdays span { display: flex; align-items: center; justify-content: flex-end; }
.heatmap-weeks { display: grid; flex: 1; gap: 4px; }
.heatmap-week { display: grid; grid-template-rows: repeat(7, auto); gap: 4px; }
.heatmap-cell {
  display: block;
  width: 100%;
  aspect-ratio: 1;
  border: 1px solid transparent;
  border-radius: 3px;
  background: var(--surface-3);
  transform-origin: center;
  transition: transform 0.12s ease, border-color 0.12s ease, box-shadow 0.12s ease;
}
.heatmap-cell.level-1 { background: color-mix(in srgb, var(--accent) 22%, var(--surface)); }
.heatmap-cell.level-2 { background: color-mix(in srgb, var(--accent) 42%, var(--surface)); }
.heatmap-cell.level-3 { background: color-mix(in srgb, var(--accent) 68%, var(--surface)); }
.heatmap-cell.level-4 { background: var(--accent-strong); border-color: color-mix(in srgb, var(--accent-strong) 72%, #000); }
.heatmap-cell.blank, .heatmap-cell.future { background: transparent; border-color: transparent; }
.heatmap-cell.future { border: 1px dashed var(--border); opacity: 0.55; }
.heatmap-cell:hover { position: relative; z-index: 2; transform: scale(1.16); border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
@media (max-width: 820px) {
  .heatmap-summary { gap: 14px; }
  .heatmap-spacer { display: none; }
}
</style>
