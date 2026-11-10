<script setup>
/** 实时告警：WeUI cells + WeUI toptips，GSAP 负责 toast 进出。 */
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { api, connectAlerts } from '../api'
import { gsap, reduceMotion } from '../motion'

const alerts = ref([])
const toasts = ref([])
const status = ref('connecting')
const toastEls = new Map()
let disconnect = null

function setToastRef(el, id) {
  if (el) toastEls.set(id, el)
  else toastEls.delete(id)
}

function push(alert) {
  alerts.value.unshift(alert)
  if (alerts.value.length > 50) alerts.value.pop()
}

async function toast(alert) {
  const id = `${Date.now()}-${Math.random()}`
  toasts.value.push({ id, ...alert })
  await nextTick()
  const el = toastEls.get(id)
  if (el && reduceMotion()) {
    el.style.opacity = '1'
  } else if (el) {
    gsap.fromTo(el, { x: 36, autoAlpha: 0, scale: 0.96 }, {
      x: 0,
      autoAlpha: 1,
      scale: 1,
      duration: 0.38,
      ease: 'back.out(1.5)'
    })
  }
  window.setTimeout(() => closeToast(id), 8000)
}

function closeToast(id) {
  const el = toastEls.get(id)
  const remove = () => {
    toasts.value = toasts.value.filter((t) => t.id !== id)
    toastEls.delete(id)
  }
  if (!el || reduceMotion()) {
    remove()
    return
  }
  gsap.to(el, { x: 30, autoAlpha: 0, scale: 0.96, duration: 0.22, ease: 'power2.in', onComplete: remove })
}

onMounted(async () => {
  try {
    const data = await api.alerts(30)
    alerts.value = data.items || []
  } catch {
    /* 历史读取失败不影响实时订阅 */
  }
  disconnect = connectAlerts(
    (msg) => {
      push(msg)
      toast(msg)
    },
    (s) => (status.value = s)
  )
})

onBeforeUnmount(() => disconnect?.())

const levelText = (lv) => (lv === 'exceeded' ? '已超限' : '预警')
const time = (t) => {
  if (!t) return ''
  const then = new Date(t).getTime()
  const diff = Math.max(0, Date.now() - then)
  const minutes = Math.floor(diff / 60000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  return new Date(t).toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' })
}
</script>

<template>
  <div class="alert-toasts">
    <div
      v-for="t in toasts"
      :key="t.id"
      :ref="(el) => setToastRef(el, t.id)"
      class="weui-toptips alert-toast"
      :class="t.level === 'exceeded' ? 'weui-toptips_warn' : ''"
      @click="closeToast(t.id)"
    >
      <div class="toast-title">{{ t.title || 'WeQuota 提醒' }}</div>
      <div class="toast-body">{{ t.message }}</div>
    </div>
  </div>

  <div class="weui-panel alert-panel">
    <div class="weui-panel__hd">
      <div>
        <strong>额度告警</strong>
        <span class="panel-sub">额度异常与实时通知</span>
      </div>
      <span
        class="weui-badge"
        :class="status === 'connected' ? '' : 'weui-badge_warn'"
      >
        {{ status === 'connected' ? '实时连接' : status === 'connecting' ? '连接中' : '已断开' }}
      </span>
    </div>
    <div class="weui-panel__bd">
      <div v-if="!alerts.length" class="empty">暂无提醒</div>
      <div v-else class="weui-cells alert-list">
        <div v-for="a in alerts" :key="a.id ?? a.created_at + a.peer_id" class="weui-cell alert-item">
          <span class="weui-cell__hd">
            <span class="weui-badge" :class="a.level === 'exceeded' ? 'weui-badge_danger' : 'weui-badge_warn'">
              {{ levelText(a.level) }}
            </span>
          </span>
          <span class="weui-cell__bd alert-peer">{{ a.peer_id }}</span>
          <span class="weui-cell__ft alert-value">
            <b class="num">{{ (a.used_tokens ?? a.used ?? 0).toLocaleString() }}</b>
            <small class="num">/ {{ (a.limit_tokens ?? a.limit ?? 0).toLocaleString() }}</small>
            <small class="alert-time">{{ time(a.created_at) }}</small>
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.alert-panel { height: 100%; }
.alert-list { max-height: 268px; overflow: auto; }
.alert-item { padding: 9px 0; gap: 8px; }
.alert-item::before { border-top-color: var(--border); }
.alert-peer { min-width: 0; overflow: hidden; color: var(--text); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.alert-value { display: flex; align-items: baseline; gap: 4px; white-space: nowrap; }
.alert-value b { color: var(--text); font-size: 12px; }
.alert-value small { color: var(--muted); font-size: 10px; }
.alert-time { margin-left: 5px; color: var(--faint) !important; }
.alert-toasts {
  position: fixed;
  right: 22px;
  bottom: 22px;
  z-index: 99;
  display: flex;
  flex-direction: column;
  gap: 9px;
  width: min(340px, calc(100vw - 44px));
}
.alert-toast {
  position: static;
  display: block;
  width: 100%;
  padding: 12px 14px;
  border-radius: 14px;
  background: var(--surface);
  border: 1px solid var(--border);
  box-shadow: var(--shadow);
  color: var(--text);
  text-align: left;
  cursor: pointer;
  opacity: 0;
}
.alert-toast.weui-toptips_warn {
  background: color-mix(in srgb, var(--danger) 8%, var(--surface));
  border-color: color-mix(in srgb, var(--danger) 38%, var(--border));
}
.toast-title { margin-bottom: 4px; color: var(--text); font-size: 12px; font-weight: 650; }
.toast-body { color: var(--muted); font-size: 11px; line-height: 1.6; }
</style>
