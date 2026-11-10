<script setup>
/**
 * 系统设置：WeUI cells / switch / navbar，字段仍由 /api/settings schema 驱动。
 * 保存后能热更新的立即生效，采集/转写类字段标记“需重启”。
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { api } from '../api'
import { gsap, reduceMotion } from '../motion'

const props = defineProps({
  open: { type: Boolean, default: false },
  page: { type: Boolean, default: false }
})
const emit = defineEmits(['close', 'saved'])

const sections = ref([])
const presets = ref([])
const values = ref({})
const draft = ref({})
const active = ref('quota')
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const notice = ref('')
const errors = ref([])
const restart = ref([])
const mailTest = ref({ state: '', message: '' })
const transcribe = ref(null)
const downloading = ref(false)
const downloadNote = ref('')
const tabPanel = ref(null)
const pageEl = ref(null)

const TAB_ORDER = ['quota', 'alert', 'smtp', 'collector', 'whisper', 'system']
const TAB_LABEL = {
  quota: '额度',
  alert: '提醒',
  smtp: '邮件',
  collector: '采集',
  whisper: '转写',
  system: '其他'
}

const tabs = computed(() =>
  TAB_ORDER.filter((id) => sections.value.some((s) => s.id === id))
)
const currentSections = computed(() =>
  sections.value.filter((s) => s.id === active.value)
)
const dirty = computed(() =>
  Object.keys(draft.value).some((k) => draft.value[k] !== values.value[k])
)

onMounted(async () => {
  await load()
  await nextTick()
  if (!pageEl.value || reduceMotion()) return
  gsap.fromTo(pageEl.value, { y: 18, autoAlpha: 0 }, {
    y: 0,
    autoAlpha: 1,
    duration: 0.5,
    ease: 'power2.out'
  })
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [data, t] = await Promise.all([
      api.settings(),
      api.transcribeStatus().catch(() => null)
    ])
    transcribe.value = t
    sections.value = data.sections || []
    presets.value = data.presets || []
    values.value = data.values || {}
    draft.value = { ...values.value }
    if (!tabs.value.includes(active.value)) active.value = tabs.value[0] || 'quota'
  } catch (e) {
    error.value = String(e.message || e)
  } finally {
    loading.value = false
  }
}

function update(key, value) {
  draft.value = { ...draft.value, [key]: value }
}

function applyPreset(preset) {
  update('alert.smtp_host', preset.host)
  update('alert.smtp_port', preset.port)
  update('alert.smtp_security', preset.security)
  notice.value = `已按「${preset.name}」填好服务器与端口；${preset.note}`
}

function onAccountBlur() {
  const account = String(draft.value['alert.smtp_user'] || '').trim()
  if (!account.includes('@')) return
  const preset = presets.value.find((p) => account.toLowerCase().endsWith('@' + p.domain))
  if (preset && !draft.value['alert.smtp_host']) applyPreset(preset)
  if (!draft.value['alert.email_from']) update('alert.email_from', account)
  if (!draft.value['alert.email_to']) update('alert.email_to', account)
}

const mailReady = computed(() => {
  const d = draft.value
  const to = String(d['alert.email_to'] || '').trim()
  return Boolean(d['alert.smtp_host'] && d['alert.smtp_port'] && d['alert.smtp_user'] && to)
})

const mailHint = computed(() => {
  const account = String(draft.value['alert.smtp_user'] || '').toLowerCase()
  if (!account.includes('@')) return null
  return presets.value.find((p) => account.endsWith('@' + p.domain)) || null
})

async function downloadModel() {
  downloading.value = true
  downloadNote.value = '正在下载，约 75MB，请不要关闭页面…'
  try {
    const res = await api.downloadModel()
    transcribe.value = res.status || transcribe.value
    downloadNote.value = res.ok ? '下载完成，可以重启模型转写' : `下载失败：${res.message}`
  } catch (e) {
    downloadNote.value = `下载失败：${String(e.message || e)}`
  } finally {
    downloading.value = false
  }
}

async function sendTestMail() {
  mailTest.value = { state: 'sending', message: '正在发送…' }
  try {
    const pending = {}
    for (const k of Object.keys(draft.value)) {
      if (draft.value[k] !== values.value[k]) pending[k] = draft.value[k]
    }
    const res = await api.testEmail(pending)
    mailTest.value = { state: res.ok ? 'ok' : 'fail', message: res.message }
  } catch (e) {
    mailTest.value = { state: 'fail', message: String(e.message || e) }
  }
}

async function save() {
  saving.value = true
  notice.value = ''
  error.value = ''
  try {
    const pending = {}
    for (const k of Object.keys(draft.value)) {
      if (draft.value[k] !== values.value[k]) pending[k] = draft.value[k]
    }
    const res = await api.saveSettings(pending)
    errors.value = res.errors || []
    restart.value = res.restart_required || []
    values.value = res.values || values.value
    draft.value = { ...values.value }
    if (!errors.value.length) {
      notice.value = restart.value.length
        ? `已保存；「${restartLabel.value}」的改动需重启后生效`
        : '已保存，立即生效'
      emit('saved')
    }
  } catch (e) {
    error.value = String(e.message || e)
  } finally {
    saving.value = false
  }
}

const restartLabel = computed(() =>
  restart.value.map((id) => TAB_LABEL[id] || id).join(' / ')
)

function reset() {
  draft.value = { ...values.value }
  errors.value = []
  notice.value = ''
  mailTest.value = { state: '', message: '' }
}

function close() {
  reset()
  emit('close')
}

watch(() => props.open, (v) => {
  if (v) load()
})

watch(active, async () => {
  await nextTick()
  if (!tabPanel.value || reduceMotion()) return
  gsap.fromTo(tabPanel.value, { y: 10, autoAlpha: 0 }, {
    y: 0,
    autoAlpha: 1,
    duration: 0.32,
    ease: 'power2.out'
  })
})
</script>

<template>
  <div v-if="open" ref="pageEl" class="settings-page">
    <div class="weui-panel settings-panel">
      <div class="weui-panel__hd settings-head">
        <div>
          <span class="settings-kicker">WEQUOTA · LOCAL SETTINGS</span>
          <strong>{{ page ? '系统设置' : '设置' }}</strong>
          <span class="panel-sub">改完点保存，能热更新的立刻生效</span>
        </div>
        <button class="weui-btn weui-btn_mini weui-btn_default" @click="close">
          {{ page ? '返回概览' : '关闭' }}
        </button>
      </div>

      <div class="weui-panel__bd settings-body">
        <div class="weui-navbar settings-tabs">
          <button
            v-for="t in tabs"
            :key="t"
            class="weui-navbar__item"
            :class="{ 'weui-bar__item_on': active === t }"
            @click="active = t"
          >
            {{ TAB_LABEL[t] || t }}
          </button>
        </div>

        <div v-if="loading" class="weui-loadmore">
          <span class="weui-primary-loading"><i></i></span>
          <span class="weui-loadmore__tips">加载中…</span>
        </div>
        <div v-else-if="error" class="weui-toptips weui-toptips_warn settings-error">{{ error }}</div>

        <div v-else ref="tabPanel" class="settings-content-panel">
          <section v-for="s in currentSections" :key="s.id">
            <div class="weui-cells weui-cells_form settings-cells">
              <div v-for="f in s.fields" :key="f.key" class="weui-cell settings-cell">
                <div class="weui-cell__hd">
                  <label class="weui-label">{{ f.label }}</label>
                </div>
                <div class="weui-cell__bd">
                  <div v-if="f.type === 'toggle'" class="settings-switch">
                    <input
                      class="weui-switch"
                      type="checkbox"
                      :checked="Boolean(draft[f.key])"
                      @change="update(f.key, $event.target.checked)"
                    />
                  </div>
                  <select
                    v-else-if="f.type === 'choice'"
                    class="weui-input"
                    :value="draft[f.key]"
                    @change="update(f.key, $event.target.value)"
                  >
                    <option v-for="o in f.options" :key="o.value" :value="o.value">{{ o.label }}</option>
                  </select>
                  <input
                    v-else-if="f.secret"
                    class="weui-input"
                    type="password"
                    autocomplete="new-password"
                    :placeholder="draft[f.key] ? '已保存，留空则不修改' : ''"
                    :value="draft[f.key]"
                    @input="update(f.key, $event.target.value)"
                  />
                  <input
                    v-else
                    class="weui-input"
                    :type="f.type === 'int' || f.type === 'number' ? 'number' : 'text'"
                    :min="f.min ?? undefined"
                    :max="f.max ?? undefined"
                    :placeholder="f.hint"
                    :value="draft[f.key]"
                    @input="update(f.key, f.type === 'int' || f.type === 'number' ? Number($event.target.value) : $event.target.value)"
                    @blur="f.key === 'alert.smtp_user' && onAccountBlur()"
                  />
                  <p v-if="f.hint && !f.secret" class="weui-cells__tips field-hint">{{ f.hint }}</p>
                </div>
                <div class="weui-cell__ft">
                  <span v-if="f.needs_restart" class="weui-badge weui-badge_warn">需重启</span>
                </div>
              </div>
            </div>

            <div v-if="s.id === 'smtp'" class="settings-block">
              <div class="settings-block-title">一键填入服务商</div>
              <div class="settings-presets">
                <button v-for="p in presets" :key="p.id" class="weui-btn weui-btn_mini weui-btn_default" @click="applyPreset(p)">
                  {{ p.name }}
                </button>
              </div>
              <div v-if="mailHint" class="weui-cells__tips settings-guide">
                <strong>{{ mailHint.name }}的密码怎么拿</strong>
                <p>{{ mailHint.note }}</p>
                <p>注意：这里填的是授权码，不是邮箱登录密码；发件人要与账号一致。</p>
              </div>
              <div class="settings-testmail">
                <button class="weui-btn weui-btn_mini weui-btn_primary" :disabled="!mailReady" @click="sendTestMail">
                  发一封测试邮件
                </button>
                <span v-if="!mailReady" class="weui-cells__tips">填好服务器、账号、收件人后可发测试邮件</span>
                <span
                  v-else-if="mailTest.state"
                  class="weui-cells__tips"
                  :class="mailTest.state === 'ok' ? 'settings-ok' : mailTest.state === 'fail' ? 'settings-bad' : ''"
                >
                  {{ mailTest.message }}
                </span>
              </div>
            </div>

            <div v-if="s.id === 'whisper'" class="settings-block">
              <div class="settings-block-title">模型状态</div>
              <p class="weui-cells__tips">
                <template v-if="!transcribe">加载中…</template>
                <template v-else-if="!transcribe.enabled">语音转写已关闭，语音消息按「时长 × 25」折算</template>
                <template v-else-if="transcribe.model_ready">已就绪（{{ transcribe.model_size }}，{{ transcribe.model_dir }}）</template>
                <template v-else>模型未下载。语音消息暂时按「时长 × 25」折算。</template>
              </p>
              <button
                class="weui-btn weui-btn_mini weui-btn_default"
                :disabled="downloading || (transcribe && !transcribe.enabled)"
                @click="downloadModel"
              >
                {{ downloading ? '下载中…' : '下载模型' }}
              </button>
              <p v-if="downloadNote" class="weui-cells__tips">{{ downloadNote }}</p>
              <p v-if="transcribe?.model_note" class="weui-cells__tips">{{ transcribe.model_note }}</p>
            </div>
          </section>
        </div>
      </div>

      <div class="weui-panel__ft settings-foot">
        <div class="settings-foot-messages">
          <template v-if="errors.length">
            <p v-for="(e, i) in errors" :key="i" class="settings-bad">{{ e }}</p>
          </template>
          <p v-else-if="notice" class="settings-ok">{{ notice }}</p>
        </div>
        <div class="panel-actions">
          <button class="weui-btn weui-btn_mini weui-btn_default" @click="reset">撤销改动</button>
          <button class="weui-btn weui-btn_mini weui-btn_default" @click="close">关闭</button>
          <button class="weui-btn weui-btn_mini weui-btn_primary" :disabled="!dirty || saving" @click="save">
            {{ saving ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.settings-page { width: 100%; }
.settings-panel { overflow: hidden; }
.settings-head { align-items: flex-start; }
.settings-kicker {
  display: block;
  margin-bottom: 5px;
  color: var(--accent-strong);
  font-size: 10px;
  font-weight: 650;
  letter-spacing: 0.12em;
}
.settings-head strong { font-size: 22px; }
.settings-body { padding: 0 0 8px; }
.settings-tabs {
  gap: 6px;
  margin-bottom: 8px;
  padding: 14px 20px 12px;
  border-bottom: 1px solid var(--border);
  background: transparent;
}
.settings-tabs .weui-navbar__item {
  flex: 1;
  min-width: 0;
  padding: 8px 4px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--surface-2);
  color: var(--muted);
  font-size: 12px;
  line-height: 1.4;
  white-space: nowrap;
  transition: background-color 0.15s ease, color 0.15s ease, border-color 0.15s ease;
}
.settings-tabs .weui-navbar__item::after { display: none; content: none; }
.settings-tabs .weui-navbar__item.weui-bar__item_on {
  border-color: var(--accent);
  background: var(--accent);
  color: var(--accent-ink);
  font-weight: 650;
  box-shadow: 0 4px 12px color-mix(in srgb, var(--accent) 22%, transparent);
}
.settings-content-panel { padding: 0 20px; }
.settings-cells { background: transparent; }
.settings-cell { padding: 12px 0; }
.settings-cell::before { border-top-color: var(--border); }
.settings-cell .weui-cell__hd { flex: 0 0 118px; }
.settings-cell .weui-cell__bd { min-width: 0; }
.settings-cell .weui-cell__ft { padding-left: 8px; }
.settings-switch { display: flex; justify-content: flex-end; }
.field-hint { padding: 4px 0 0; }
.settings-block {
  margin-top: 14px;
  padding-top: 14px;
  border-top: 1px solid var(--border);
}
.settings-block-title { margin-bottom: 8px; color: var(--text); font-size: 12px; font-weight: 650; }
.settings-presets { display: flex; flex-wrap: wrap; gap: 6px; }
.settings-guide { padding-top: 10px; }
.settings-guide strong { display: block; margin-bottom: 4px; color: var(--text); }
.settings-guide p { margin: 2px 0; }
.settings-testmail { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 10px; }
.settings-error {
  display: block;
  position: static;
  margin: 12px 20px;
  border-radius: 10px;
  text-align: left;
}
.settings-ok { color: var(--ok); }
.settings-bad { color: var(--danger); }
.settings-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  padding: 12px 20px;
  border-top: 1px solid var(--border);
  background: var(--surface-2);
}
.settings-foot-messages { min-width: 0; color: var(--muted); font-size: 11px; }
.settings-foot-messages p { margin: 0; }
.settings-foot .panel-actions { flex: 0 0 auto; }
@media (max-width: 720px) {
  .settings-cell { align-items: flex-start; flex-direction: column; gap: 5px; }
  .settings-cell .weui-cell__hd { flex: none; }
  .settings-cell .weui-cell__ft { padding-left: 0; }
  .settings-switch { justify-content: flex-start; }
  .settings-foot { align-items: flex-start; }
}
</style>
