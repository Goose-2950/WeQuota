<script setup>
/**
 * 联系人额度弹窗：WeUI dialog + cells + switch，GSAP 负责弹入/收起。
 * 仍支持新建/编辑/删除、窗口类型、预警阈值和启停。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../api'
import Icon from './Icon.vue'
import { gsap, reduceMotion } from '../motion'

const props = defineProps({
  open: { type: Boolean, default: false },
  item: { type: Object, default: null },
  peers: { type: Array, default: () => [] }
})
const emit = defineEmits(['close', 'saved'])

const WINDOWS = [
  { value: 'daily', label: '每日', hint: '每天零点重置' },
  { value: 'weekly', label: '每周', hint: '每周一零点重置' },
  { value: 'monthly', label: '每月', hint: '每月 1 号零点重置' }
]
const QUICK = [1000, 5000, 10000, 50000]

const peerId = ref('')
const limit = ref(5000)
const windowType = ref('daily')
const warnRatio = ref(0.8)
const enabled = ref(true)
const saving = ref(false)
const removing = ref(false)
const error = ref('')
const notice = ref('')
const limitInput = ref(null)
const dialogRef = ref(null)
const peerOpen = ref(false)
const peerQuery = ref('')
const peerSearch = ref(null)
const peerPanel = ref(null)
let previousOverflow = ''
let closing = false

const isNew = computed(() => !props.item?.peer_id)
const peersWithoutRule = computed(() => props.peers || [])
const filteredPeerChoices = computed(() => {
  const q = peerQuery.value.trim().toLowerCase()
  const list = peersWithoutRule.value.filter((peer) =>
    !q || `${peer.peer_name || ''} ${peer.peer_id || ''}`.toLowerCase().includes(q)
  )
  return list.slice(0, 200)
})
const peerChoiceCount = computed(() => peersWithoutRule.value.filter((peer) => {
  const q = peerQuery.value.trim().toLowerCase()
  return !q || `${peer.peer_name || ''} ${peer.peer_id || ''}`.toLowerCase().includes(q)
}).length)
const selectedPeerLabel = computed(() => {
  const hit = peersWithoutRule.value.find((peer) => peer.peer_id === peerId.value)
  return hit?.peer_name || hit?.peer_id || ''
})

watch(
  () => [props.open, props.item],
  async ([open]) => {
    if (!open) return
    closing = false
    error.value = ''
    notice.value = ''
    peerOpen.value = false
    peerQuery.value = ''
    if (props.item?.peer_id) {
      peerId.value = props.item.peer_id
      limit.value = Number(props.item.limit || 5000)
      windowType.value = props.item.window_type || 'daily'
      warnRatio.value = Number(props.item.warn_ratio ?? 0.8)
      enabled.value = props.item.enabled !== false
    } else {
      peerId.value = peersWithoutRule.value[0]?.peer_id || ''
      limit.value = 5000
      windowType.value = 'daily'
      warnRatio.value = 0.8
      enabled.value = true
    }
    await nextTick()
    limitInput.value?.focus?.()
    limitInput.value?.select?.()
  },
  { immediate: true }
)

watch(
  () => props.open,
  async (open) => {
    if (typeof document === 'undefined') return
    if (open) {
      previousOverflow = document.body.style.overflow
      document.body.style.overflow = 'hidden'
      window.addEventListener('keydown', onKeydown)
      await nextTick()
      if (!reduceMotion() && dialogRef.value) {
        gsap.fromTo(dialogRef.value, { y: 20, scale: 0.94, autoAlpha: 0 }, {
          y: 0,
          scale: 1,
          autoAlpha: 1,
          duration: 0.34,
          ease: 'back.out(1.5)'
        })
      }
    } else {
      document.body.style.overflow = previousOverflow
      window.removeEventListener('keydown', onKeydown)
    }
  }
)

onBeforeUnmount(() => {
  if (typeof document !== 'undefined') document.body.style.overflow = previousOverflow
  window.removeEventListener('keydown', onKeydown)
})

function onKeydown(event) {
  if (event.key === 'Escape') close()
}

const selectedName = computed(() => {
  const hit = peersWithoutRule.value.find((p) => p.peer_id === peerId.value)
  return hit?.peer_name || props.item?.peer_name || peerId.value || ''
})

const warnPercent = computed(() => Math.round(warnRatio.value * 100))
const windowHint = computed(
  () => WINDOWS.find((item) => item.value === windowType.value)?.hint || ''
)

const warnRatioRaw = computed({
  get: () => Math.round(warnRatio.value * 100),
  set: (value) => { warnRatio.value = Number(value) / 100 }
})

async function togglePeerSelect() {
  peerOpen.value = !peerOpen.value
  if (!peerOpen.value) return
  peerQuery.value = ''
  await nextTick()
  peerSearch.value?.focus?.()
  if (!reduceMotion() && peerPanel.value) {
    gsap.fromTo(peerPanel.value, { y: -6, autoAlpha: 0 }, {
      y: 0,
      autoAlpha: 1,
      duration: 0.22,
      ease: 'power2.out'
    })
  }
}

function choosePeer(peer) {
  peerId.value = peer.peer_id
  peerOpen.value = false
}

async function submit() {
  if (!peerId.value) {
    error.value = '请先选择联系人'
    return
  }
  if (!Number.isFinite(limit.value) || limit.value <= 0) {
    error.value = '额度要填大于 0 的数字'
    return
  }
  saving.value = true
  error.value = ''
  try {
    await api.setRule({
      peer_id: peerId.value,
      peer_name: selectedName.value,
      window_type: windowType.value,
      limit_tokens: Math.round(limit.value),
      warn_ratio: warnRatio.value,
      enabled: enabled.value
    })
    notice.value = '已保存，当前窗口已按新额度重新评估'
    emit('saved')
    window.setTimeout(close, 500)
  } catch (e) {
    error.value = String(e.message || e)
  } finally {
    saving.value = false
  }
}

async function remove() {
  if (!props.item?.peer_id) return
  removing.value = true
  error.value = ''
  try {
    await api.deleteRule(props.item.peer_id)
    emit('saved')
    close()
  } catch (e) {
    error.value = String(e.message || e)
  } finally {
    removing.value = false
  }
}

function close() {
  if (closing) return
  closing = true
  if (reduceMotion() || !dialogRef.value) {
    closing = false
    emit('close')
    return
  }
  gsap.to(dialogRef.value, {
    y: 12,
    scale: 0.97,
    autoAlpha: 0,
    duration: 0.18,
    ease: 'power2.in',
    onComplete: () => {
      closing = false
      emit('close')
    }
  })
}
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="weui-mask quota-mask" @click.self="close">
      <section ref="dialogRef" class="weui-dialog quota-dialog" role="dialog" aria-modal="true" aria-labelledby="quota-dialog-title">
        <div class="weui-dialog__hd quota-head">
          <span class="quota-kicker">联系人额度</span>
          <strong id="quota-dialog-title" class="weui-dialog__title">{{ isNew ? '设置额度' : '调整额度' }}</strong>
          <p>{{ selectedName || '选择一个联系人开始配置' }}</p>
          <button class="weui-btn weui-btn_icon quota-close" aria-label="关闭" @click="close">
            <Icon name="close" :size="16" />
          </button>
        </div>

        <div class="weui-dialog__bd quota-body" @click="peerOpen = false">
          <div class="weui-cells weui-cells_form quota-cells">
            <div v-if="isNew" class="weui-cell quota-peer-cell" @click.stop>
              <span class="weui-cell__hd"><span class="weui-label">联系人</span></span>
              <span class="weui-cell__bd quota-peer-field">
                <button class="quota-peer-trigger" :class="{ open: peerOpen }" @click.stop="togglePeerSelect">
                  <span>{{ selectedPeerLabel || '选择联系人' }}</span>
                  <Icon name="chevron" :size="14" class="quota-peer-arrow" />
                </button>
                <div v-if="peerOpen" ref="peerPanel" class="quota-peer-panel" @click.stop>
                  <label class="quota-peer-search">
                    <Icon name="search" :size="14" />
                    <input
                      ref="peerSearch"
                      v-model="peerQuery"
                      type="search"
                      placeholder="搜索联系人…"
                      autocomplete="off"
                    />
                  </label>
                  <div class="quota-peer-options">
                    <button
                      v-for="p in filteredPeerChoices"
                      :key="p.peer_id"
                      class="quota-peer-option"
                      :class="{ active: p.peer_id === peerId }"
                      @click="choosePeer(p)"
                    >
                      <span class="quota-peer-avatar">{{ (p.peer_name || p.peer_id).slice(0, 1).toUpperCase() }}</span>
                      <span class="quota-peer-name">{{ p.peer_name || p.peer_id }}</span>
                      <span v-if="p.peer_id === peerId" class="quota-peer-check">✓</span>
                    </button>
                    <div v-if="!filteredPeerChoices.length" class="empty">没有匹配的联系人</div>
                  </div>
                  <div v-if="peerChoiceCount > filteredPeerChoices.length" class="quota-peer-more">
                    还有 {{ peerChoiceCount - filteredPeerChoices.length }} 人，输入关键字继续筛选
                  </div>
                </div>
              </span>
            </div>
            <div v-else class="weui-cell">
              <span class="weui-cell__hd"><span class="weui-label">联系人</span></span>
              <span class="weui-cell__bd quota-fixed">{{ selectedName }}</span>
              <span class="weui-cell__ft">{{ peerId }}</span>
            </div>

            <label class="weui-cell">
              <span class="weui-cell__hd"><span class="weui-label">窗口额度</span></span>
              <span class="weui-cell__bd">
                <input
                  ref="limitInput"
                  v-model.number="limit"
                  class="weui-input quota-limit-input num"
                  type="number"
                  min="1"
                  step="100"
                />
              </span>
              <span class="weui-cell__ft">token</span>
            </label>

            <div class="weui-cell quota-window-cell">
              <span class="weui-cell__hd"><span class="weui-label">统计窗口</span></span>
              <span class="weui-cell__bd">
                <span class="quota-window">
                  <button
                    v-for="w in WINDOWS"
                    :key="w.value"
                    class="weui-btn weui-btn_mini"
                    :class="windowType === w.value ? 'weui-btn_primary' : 'weui-btn_default'"
                    @click="windowType = w.value"
                  >{{ w.label }}</button>
                </span>
              </span>
            </div>

            <div class="weui-cell quota-quick-cell">
              <span class="weui-cell__hd"><span class="weui-label">快捷额度</span></span>
              <span class="weui-cell__bd quota-quick">
                <button v-for="q in QUICK" :key="q" class="weui-btn weui-btn_mini weui-btn_default num" @click="limit = q">
                  {{ q.toLocaleString() }}
                </button>
              </span>
            </div>

            <div class="weui-cell quota-range-cell">
              <span class="weui-cell__hd"><span class="weui-label">提醒阈值</span></span>
              <span class="weui-cell__bd">
                <input v-model.number="warnRatioRaw" class="quota-range" type="range" min="10" max="100" step="5" />
              </span>
              <span class="weui-cell__ft num">{{ warnPercent }}%</span>
            </div>

            <div class="weui-cell">
              <span class="weui-cell__bd quota-switch-copy">
                <strong>启用这条规则</strong>
                <small>停用后不再计入提醒，已产生的用量仍会统计</small>
              </span>
              <span class="weui-cell__ft">
                <input class="weui-switch" type="checkbox" :checked="enabled" @change="enabled = $event.target.checked" />
              </span>
            </div>
          </div>

          <p class="weui-cells__tips quota-hint">
            约 {{ Math.round(limit / 1.6).toLocaleString() }} 个汉字；{{ windowHint }}；
            约 {{ Math.round(limit * warnRatio).toLocaleString() }} token 时预警
            <template v-if="item"> · 当前已用 {{ (item.used ?? 0).toLocaleString() }}</template>
          </p>
          <p v-if="error" class="quota-message error">{{ error }}</p>
          <p v-else-if="notice" class="quota-message ok">{{ notice }}</p>
        </div>

        <div class="weui-dialog__ft quota-ft">
          <button v-if="!isNew" class="weui-dialog__btn weui-dialog__btn_warn" :disabled="removing" @click="remove">
            {{ removing ? '删除中…' : '删除' }}
          </button>
          <button class="weui-dialog__btn weui-dialog__btn_default" @click="close">取消</button>
          <button class="weui-dialog__btn weui-dialog__btn_primary" :disabled="saving" @click="submit">
            {{ saving ? '保存中…' : '保存额度' }}
          </button>
        </div>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.quota-mask {
  display: grid;
  place-items: center;
  padding: 20px;
}
.quota-dialog {
  display: flex;
  flex-direction: column;
  width: min(620px, 100%);
  max-height: calc(100vh - 40px);
  text-align: left;
}
.quota-head {
  position: relative;
  padding: 20px 22px 8px;
  text-align: left;
  background: radial-gradient(circle at 12% 0%, var(--accent-soft), transparent 55%);
}
.quota-kicker {
  display: block;
  margin-bottom: 5px;
  color: var(--accent-strong);
  font-size: 10px;
  font-weight: 650;
  letter-spacing: 0.1em;
}
.quota-head p {
  margin: 5px 0 0;
  color: var(--muted);
  font-size: 11px;
}
.quota-close {
  position: absolute;
  top: 16px;
  right: 16px;
  width: 30px;
  height: 30px;
  border-color: var(--border);
}
.quota-body {
  overflow-y: auto;
  padding: 4px 22px 12px;
}
.quota-cells { background: transparent; }
.quota-cells .weui-cell { padding: 10px 0; }
.quota-cells .weui-cell::before { border-top-color: var(--border); }
.quota-cells .weui-cell__hd { flex: 0 0 88px; margin-right: 12px; }
.quota-limit-input { font-size: 19px; font-weight: 700; letter-spacing: -0.02em; }
.quota-fixed { color: var(--text); font-weight: 600; }
.quota-peer-field { position: relative; }
.quota-peer-trigger {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
  min-height: 32px;
  padding: 0 2px;
  border: 0;
  background: transparent;
  color: var(--text);
  font-size: 13px;
  font-weight: 600;
  text-align: left;
}
.quota-peer-trigger.open { color: var(--accent-strong); }
.quota-peer-arrow { color: var(--faint); transition: transform 0.18s ease; }
.quota-peer-trigger.open .quota-peer-arrow { transform: rotate(90deg); }
.quota-peer-panel {
  position: absolute;
  z-index: 20;
  top: calc(100% + 6px);
  right: 0;
  left: 0;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--surface);
  box-shadow: var(--shadow);
}
.quota-peer-search {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 8px;
  padding: 7px 10px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--surface-2);
  color: var(--faint);
}
.quota-peer-search input {
  min-width: 0;
  flex: 1;
  border: 0;
  outline: 0;
  background: transparent;
  color: var(--text);
  font-size: 12px;
}
.quota-peer-options { max-height: 220px; overflow-y: auto; padding: 0 4px 4px; }
.quota-peer-option {
  display: flex;
  align-items: center;
  gap: 9px;
  width: 100%;
  min-height: 38px;
  padding: 0 8px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--text);
  text-align: left;
}
.quota-peer-option:hover { background: var(--surface-2); }
.quota-peer-option.active { background: var(--accent-soft); color: var(--accent-strong); }
.quota-peer-avatar {
  display: grid;
  place-items: center;
  width: 22px;
  height: 22px;
  flex: 0 0 22px;
  border-radius: 7px;
  background: var(--surface-2);
  color: var(--muted);
  font-size: 9px;
  font-weight: 700;
}
.quota-peer-option.active .quota-peer-avatar { background: var(--accent); color: var(--accent-ink); }
.quota-peer-name { min-width: 0; flex: 1; overflow: hidden; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.quota-peer-check { color: var(--accent-strong); font-size: 13px; font-weight: 700; }
.quota-peer-more { padding: 7px 10px 9px; color: var(--faint); font-size: 10px; text-align: center; }
.quota-window, .quota-quick { display: flex; flex-wrap: wrap; gap: 6px; justify-content: flex-end; }
.quota-window-cell .weui-cell__bd { text-align: right; }
.quota-quick-cell .weui-cell__bd { text-align: right; }
.quota-range { width: 100%; accent-color: var(--accent); }
.quota-switch-copy { display: flex; flex-direction: column; gap: 3px; }
.quota-switch-copy strong { color: var(--text); font-size: 12px; }
.quota-switch-copy small { color: var(--muted); font-size: 10px; line-height: 1.5; }
.quota-hint { padding: 8px 0 0; text-align: left; }
.quota-message { margin: 8px 0 0; font-size: 11px; }
.quota-message.error { color: var(--danger); }
.quota-message.ok { color: var(--ok); }
.quota-ft .weui-dialog__btn { padding: 15px 8px; }
.quota-ft .weui-dialog__btn[disabled] { opacity: 0.55; }
@media (max-width: 640px) {
  .quota-mask { padding: 12px; }
  .quota-dialog { max-height: calc(100vh - 24px); }
}
</style>
