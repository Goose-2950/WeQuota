<script setup>
/** 联系人额度：WeUI 列表行 + progress，点击弹出额度卡片。 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { gsap, reduceMotion } from '../motion'

const props = defineProps({
  items: { type: Array, default: () => [] }
})
const emit = defineEmits(['edit', 'add'])

const panelEl = ref(null)

const cards = computed(() =>
  props.items.map((u) => {
    const ratio = u.limit ? Math.min(u.ratio ?? u.used / u.limit, 1) : 0
    const level = !u.limit ? 'none' : (u.ratio ?? 0) >= 1 ? 'danger' : (u.ratio ?? 0) >= 0.8 ? 'warn' : 'ok'
    return { ...u, ratio, level }
  })
)

function edit(item) {
  emit('edit', item)
}

function animateBars() {
  if (!panelEl.value || reduceMotion()) return
  const bars = panelEl.value.querySelectorAll('.quota-fill')
  if (!bars.length) return
  gsap.fromTo(bars, { scaleX: 0 }, {
    scaleX: 1,
    duration: 0.65,
    stagger: 0.05,
    ease: 'power2.out',
    overwrite: true
  })
}

onMounted(animateBars)
watch(cards, async () => {
  await nextTick()
  animateBars()
})
</script>

<template>
  <div ref="panelEl" class="weui-panel quota-panel">
    <div class="weui-panel__hd">
      <div>
        <strong>额度剩余</strong>
        <span class="panel-sub">点击条目即可调整联系人额度</span>
      </div>
      <div class="panel-actions">
        <span class="weui-badge weui-badge_plain">实时</span>
        <button class="weui-btn weui-btn_mini weui-btn_primary" @click="emit('add')">+ 新增</button>
      </div>
    </div>
    <div class="weui-panel__bd">
      <div v-if="!cards.length" class="empty">
        还没有配置额度——点右上角「新增」给联系人设一个，或先设一个全局默认额度
      </div>
      <div v-else class="quota-list">
        <div
          v-for="c in cards"
          :key="c.peer_id"
          class="quota-row"
          :class="c.level"
          @click="edit(c)"
        >
          <div class="row-main">
            <div class="name">{{ c.peer_name || c.peer_id }}</div>
            <div class="used num">
              {{ (c.used ?? 0).toLocaleString() }}
              <span class="limit">/ {{ c.limit ? c.limit.toLocaleString() : '未设额度' }}</span>
            </div>
          </div>
          <div class="weui-progress quota-progress">
            <div class="weui-progress__bar">
              <div
                class="weui-progress__inner-bar quota-fill"
                :style="{ width: `${c.ratio * 100}%`, background: c.level === 'danger' ? 'var(--danger)' : c.level === 'warn' ? 'var(--warn)' : 'var(--accent)' }"
              ></div>
            </div>
          </div>
          <div class="foot">
            <span
              class="weui-badge"
              :class="c.level === 'danger' ? 'weui-badge_danger' : c.level === 'warn' ? 'weui-badge_warn' : c.level === 'none' ? 'weui-badge_plain' : ''"
            >
              {{ c.level === 'danger' ? '已超限' : c.level === 'warn' ? '接近上限' : c.level === 'ok' ? '正常' : '未设置' }}
            </span>
            <span class="num remain">{{ c.limit ? `剩 ${c.remaining.toLocaleString()}` : '—' }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.quota-panel { height: 100%; }
.quota-list { display: flex; flex-direction: column; }
.quota-row { padding: 13px 0; border-bottom: 1px solid var(--border); cursor: pointer; transition: border-color 0.15s ease, background 0.15s ease; }
.quota-row:first-child { padding-top: 0; }
.quota-row:last-child { padding-bottom: 0; border-bottom: 0; }
.quota-row:hover { border-color: var(--border-strong); }
.row-main { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; }
.name { margin-bottom: 7px; color: var(--text); font-size: 13px; font-weight: 650; }
.used { color: var(--text); font-size: 21px; font-weight: 700; white-space: nowrap; }
.limit { color: var(--muted); font-size: 12px; font-weight: 400; }
.quota-progress { margin: 10px 0 9px; }
.quota-fill { transform-origin: left center; }
.foot { display: flex; align-items: center; justify-content: space-between; font-size: 12px; }
.remain { color: var(--muted); }
</style>
