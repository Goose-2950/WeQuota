<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api, account as accountStore, auth } from './api'
import Icon from './components/Icon.vue'
import QuotaCards from './components/QuotaCards.vue'
import RankingBoard from './components/RankingBoard.vue'
import TimeSeriesPanel from './components/TimeSeriesPanel.vue'
import TypePie from './components/TypePie.vue'
import ReportPanel from './components/ReportPanel.vue'
import AlertPanel from './components/AlertPanel.vue'
import SettingsPanel from './components/SettingsPanel.vue'
import QuotaDialog from './components/QuotaDialog.vue'
import KeyMetricsPanel from './components/KeyMetricsPanel.vue'
import MessageHeatmap from './components/MessageHeatmap.vue'
import CollectionProgress from './components/CollectionProgress.vue'
import CountUp from './components/CountUp.vue'
import WeuiBackdrop from './components/WeuiBackdrop.vue'
import { gsap, ScrollTrigger, reduceMotion, scrollToTarget } from './motion'
import { isDark, toggleTheme } from './theme'

const appRoot = ref(null)
const identityDialog = ref(null)
let introCtx = null
let refreshTimer = null
let refreshTriggerTimer = null

const summary = ref({ tokens: 0, messages: 0, peers: 0, alerts: 0, top_peer: null })
const ranking = ref([])
const usage = ref([])
const types = ref([])
const version = ref('')
const selectedPeer = ref('')
const days = ref(7)
const error = ref('')
const accounts = ref([])
const accountId = ref('')
const collector = ref(null)
const view = ref('dashboard')
const showIdentity = ref(false)
const avatarFailed = ref(false)
const merging = ref(false)
const mergeError = ref('')
const selectedAccounts = ref([])
const editing = ref(null)
const drawerOpen = ref(false)
const peerOptions = ref([])
const needToken = ref(false)
const tokenInput = ref('')
const tokenError = ref('')
const query = ref('')
const searchInput = ref(null)
const sidebarOpen = ref(false)
const activeNav = ref('overview')
const syncing = ref(false)
const syncNote = ref('')
const syncStatus = ref(null)
const peerListEl = ref(null)
const peerScrollTop = ref(0)
const PEER_ROW_HEIGHT = 46
const PEER_VIEWPORT = 330
let statusTimer = null

const RANGES = [
  { key: 1, label: '今日' },
  { key: 7, label: '近 7 天' },
  { key: 30, label: '近 30 天' }
]

const primaryNav = [
  { id: 'overview', label: '概览', icon: 'grid' },
  { id: 'timeseries', label: '时段分析', icon: 'chart' },
  { id: 'frequency', label: '消息频率', icon: 'calendar' },
  { id: 'quota', label: '额度', icon: 'wallet' },
  { id: 'alerts', label: '提醒', icon: 'bell' },
  { id: 'ranking', label: '联系人', icon: 'users' },
  { id: 'report', label: '报表', icon: 'report' }
]

const secondaryNav = [
  { id: 'service', label: '采集状态', icon: 'shield' },
  { id: 'settings', label: '设置', icon: 'settings' },
  { id: 'account', label: '账号', icon: 'users' }
]

const rangeLabel = computed(() => RANGES.find((item) => item.key === days.value)?.label || '')
const activeLabel = computed(() => {
  if (view.value === 'settings') return '设置'
  return [...primaryNav, ...secondaryNav].find((item) => item.id === activeNav.value)?.label || '概览'
})

const currentAccount = computed(() => {
  const hit = accounts.value.find((item) => item.account_id === accountId.value)
  return hit || {
    account_id: accountId.value,
    display_name: '',
    wechat_id: accountId.value,
    avatar_url: '',
    stable_id: '',
    messages: 0
  }
})
const accountName = computed(() =>
  currentAccount.value.display_name || currentAccount.value.account_id || '未选择账号'
)
const accountWechatId = computed(() =>
  currentAccount.value.wechat_id || currentAccount.value.account_id || '未获取'
)
const accountInitial = computed(() => accountName.value.slice(0, 1).toUpperCase() || 'W')
const accountAliases = computed(() => {
  const stable = currentAccount.value.stable_id
  if (!stable) return []
  return accounts.value.filter((item) => item.stable_id === stable)
})
const mergeTarget = computed(() => {
  const group = accountAliases.value
  if (!group.length) return null
  return [...group].sort((a, b) => {
    const byMessage = String(b.last_message_at || '').localeCompare(String(a.last_message_at || ''))
    if (byMessage) return byMessage
    const bySync = String(b.last_sync_at || '').localeCompare(String(a.last_sync_at || ''))
    if (bySync) return bySync
    const byUpdated = String(b.updated_at || '').localeCompare(String(a.updated_at || ''))
    if (byUpdated) return byUpdated
    return Number(b.messages || 0) - Number(a.messages || 0)
  })[0]
})

watch(accountId, () => { avatarFailed.value = false })
watch(view, async (value) => {
  if (value !== 'dashboard') return
  await nextTick()
  animateDashboard()
})
watch(showIdentity, async (open) => {
  if (open) {
    mergeError.value = ''
    selectedAccounts.value = []
    await nextTick()
    if (!reduceMotion() && identityDialog.value) {
      gsap.fromTo(
        identityDialog.value,
        { y: 18, scale: 0.94, autoAlpha: 0 },
        { y: 0, scale: 1, autoAlpha: 1, duration: 0.34, ease: 'back.out(1.5)' }
      )
    }
  }
})

function pulse(event) {
  const target = event?.currentTarget
  if (!target || reduceMotion()) return
  gsap.fromTo(target, { scale: 1 }, {
    scale: 0.96,
    duration: 0.09,
    yoyo: true,
    repeat: 1,
    ease: 'power1.inOut'
  })
}

function animateDashboard() {
  if (!appRoot.value) return
  introCtx?.revert()
  introCtx = gsap.context(() => {
    if (reduceMotion()) return
    gsap.from('.page-heading .page-title > *', {
      y: 20,
      autoAlpha: 0,
      duration: 0.6,
      stagger: 0.08,
      ease: 'power3.out'
    })
    gsap.from('.account-summary', {
      y: 12,
      autoAlpha: 0,
      duration: 0.5,
      delay: 0.16,
      ease: 'power2.out'
    })
    gsap.from('.collect-card', {
      y: 18,
      autoAlpha: 0,
      duration: 0.5,
      delay: 0.2,
      ease: 'power2.out'
    })
    gsap.from('.kpi-card', {
      y: 22,
      autoAlpha: 0,
      duration: 0.48,
      delay: 0.26,
      stagger: 0.06,
      ease: 'power2.out'
    })
    gsap.from('.hero-stack > div', {
      y: 22,
      autoAlpha: 0,
      duration: 0.55,
      delay: 0.34,
      stagger: 0.08,
      ease: 'power2.out'
    })
    gsap.from('.sidebar .nav-item', {
      x: -12,
      autoAlpha: 0,
      duration: 0.35,
      delay: 0.05,
      stagger: 0.025,
      ease: 'power2.out'
    })
    ScrollTrigger.batch('.dashboard-grid > div', {
      start: 'top 92%',
      once: true,
      onEnter: (batch) => gsap.fromTo(
        batch,
        { y: 24, autoAlpha: 0 },
        { y: 0, autoAlpha: 1, duration: 0.52, stagger: 0.07, ease: 'power2.out', overwrite: true }
      )
    })
  }, appRoot.value)
  ScrollTrigger.refresh()
}

async function mergeAliases() {
  const stable = currentAccount.value.stable_id
  if (!stable || accountAliases.value.length < 2) return
  const latest = mergeTarget.value
  const ok = window.confirm(
    `将 ${accountAliases.value.length} 个同身份账号合并到最新账号「${latest?.display_name || latest?.account_id}」？\n\n` +
    '合并会迁移消息、额度、提醒和游标，并写入别名记录；合并前请确认这些账号确实是同一份聊天记录。'
  )
  if (!ok) return
  merging.value = true
  mergeError.value = ''
  try {
    const result = await api.mergeAccounts({
      stable_id: stable,
      target_account_id: latest?.account_id || null
    })
    accountId.value = result.target
    await loadAccounts()
    await loadAll()
    showIdentity.value = false
    syncNote.value = `已合并 ${result.merged.length} 个历史别名`
  } catch (e) {
    mergeError.value = String(e.message || e)
  } finally {
    merging.value = false
  }
}

async function mergeSelected() {
  if (selectedAccounts.value.length < 2) return
  const ok = window.confirm(
    `将选中的 ${selectedAccounts.value.length} 个账号合并到其中最新活跃的账号？\n\n` +
    '请确认它们确实是同一份微信聊天记录；合并会迁移消息、额度、提醒和游标。'
  )
  if (!ok) return
  merging.value = true
  mergeError.value = ''
  try {
    const result = await api.mergeAccounts({ account_ids: selectedAccounts.value })
    accountId.value = result.target
    selectedAccounts.value = []
    await loadAccounts()
    await loadAll()
    showIdentity.value = false
    syncNote.value = `已合并 ${result.merged.length} 个历史别名`
  } catch (e) {
    mergeError.value = String(e.message || e)
  } finally {
    merging.value = false
  }
}

const selectedName = computed(() => {
  const hit = ranking.value.find((r) => r.peer_id === selectedPeer.value)
  if (hit) return hit.peer_name || hit.peer_id
  const peer = peerOptions.value.find((p) => p.peer_id === selectedPeer.value)
  return peer ? peer.peer_name || peer.peer_id : selectedPeer.value
})

const filteredRanking = computed(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return ranking.value
  return ranking.value.filter((item) =>
    `${item.peer_name || ''} ${item.peer_id || ''}`.toLowerCase().includes(q)
  )
})

const filteredPeers = computed(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return peerOptions.value
  return peerOptions.value.filter((item) =>
    `${item.peer_name || ''} ${item.peer_id || ''}`.toLowerCase().includes(q)
  )
})

const peerWindowStart = computed(() =>
  Math.max(0, Math.floor(peerScrollTop.value / PEER_ROW_HEIGHT) - 3)
)
const peerWindowItems = computed(() => {
  const start = peerWindowStart.value
  const count = Math.ceil(PEER_VIEWPORT / PEER_ROW_HEIGHT) + 7
  return filteredPeers.value.slice(start, start + count).map((peer, index) => ({
    ...peer,
    _top: (start + index) * PEER_ROW_HEIGHT
  }))
})
const peerTotalHeight = computed(() => filteredPeers.value.length * PEER_ROW_HEIGHT)

function onPeerScroll(event) {
  peerScrollTop.value = event.target.scrollTop
}

watch(query, () => {
  peerScrollTop.value = 0
  if (peerListEl.value) peerListEl.value.scrollTop = 0
})

const filteredUsage = computed(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return usage.value
  return usage.value.filter((item) =>
    `${item.peer_name || ''} ${item.peer_id || ''}`.toLowerCase().includes(q)
  )
})

const kpis = computed(() => [
  { label: 'Token 总量', number: Number(summary.value.tokens || 0), numeric: true, hint: rangeLabel.value },
  { label: '消息数', number: Number(summary.value.messages || 0), numeric: true, hint: rangeLabel.value },
  { label: '活跃联系人', number: Number(summary.value.peers || 0), numeric: true, hint: '当前统计范围' },
  {
    label: '消耗最高',
    value: summary.value.top_peer?.peer_name || summary.value.top_peer?.peer_id || '—',
    numeric: false,
    hint: '当前区间',
    compact: true
  },
  { label: '提醒次数', number: Number(summary.value.alerts || 0), numeric: true, hint: '额度异常提醒' }
])

const collectorTone = computed(() => {
  const state = syncStatus.value?.status || collector.value?.worker?.status
  return { running: 'ok', sleeping: 'warn', error: 'danger' }[state] || 'off'
})

const collectorLabel = computed(() => {
  const c = collector.value
  if (!c) return '采集器未连接'
  const mode = c.mode === 'demo' ? '示例数据' : 'wechat-cli'
  const state = syncStatus.value?.status || c.worker?.status
  const text = { running: '采集中', sleeping: '微信未登录，休眠', error: '采集异常', idle: '待启动' }[state] || '待启动'
  return `${mode} / ${text}`
})

const collectorNavText = computed(() => {
  const worker = syncStatus.value || collector.value?.worker
  const state = worker?.status
  if (state === 'sleeping') return '休眠'
  if (state === 'error') return '异常'
  if (worker?.progress?.stage === 'checking') return '检查中'
  if (state === 'running') return '采集中'
  return '待启动'
})

async function loadAll() {
  try {
    const [s, r, u, t, c, p] = await Promise.all([
      api.summary(days.value),
      api.ranking(days.value, 10),
      api.usage(),
      api.byType(days.value, selectedPeer.value),
      api.collectorStatus().catch(() => null),
      api.peers().catch(() => ({ items: [] }))
    ])
    summary.value = s
    ranking.value = r.items || []
    usage.value = u.items || []
    types.value = t.items || []
    if (c) collector.value = c
    peerOptions.value = p.items || []
    error.value = ''
  } catch (e) {
    if (e && e.status === 401) {
      needToken.value = true
      tokenError.value = '访问令牌无效或已失效'
    } else {
      error.value = String(e.message || e)
    }
  }
}

async function loadAccounts() {
  try {
    const data = await api.accounts()
    accounts.value = data.items || []
    if (!accountId.value) {
      accountId.value = data.default || (accounts.value[0] && accounts.value[0].account_id) || ''
    }
    accountStore.set(accountId.value)
  } catch (e) {
    if (e && e.status === 401) needToken.value = true
  }
}

async function loadSyncStatus() {
  try {
    syncStatus.value = await api.syncStatus()
  } catch {
    /* 采集状态不影响大屏其它模块 */
  }
}

function submitToken() {
  const token = tokenInput.value.trim()
  if (!token) {
    tokenError.value = '请输入访问令牌'
    return
  }
  auth.set(token)
  needToken.value = false
  tokenError.value = ''
  error.value = ''
  loadAccounts()
  loadAll()
}

function switchAccount() {
  accountStore.set(accountId.value)
  selectedPeer.value = ''
  loadAll()
}

function selectPeer(row) {
  selectedPeer.value = selectedPeer.value === row.peer_id ? '' : row.peer_id
  loadAll()
}

function clearPeer() {
  selectedPeer.value = ''
  loadAll()
}

function openEdit(item) {
  editing.value = item
  drawerOpen.value = true
}

function openAdd() {
  editing.value = null
  drawerOpen.value = true
}

async function onQuotaSaved() {
  await loadAll()
}

function go(target) {
  sidebarOpen.value = false
  if (target === 'settings') {
    view.value = 'settings'
    activeNav.value = 'settings'
    window.scrollTo({ top: 0, behavior: 'smooth' })
    return
  }
  if (view.value !== 'dashboard') view.value = 'dashboard'
  activeNav.value = target
  nextTick(() => scrollToTarget(document.getElementById(target)))
}

function closeSettings() {
  view.value = 'dashboard'
  activeNav.value = 'overview'
}

async function runSync(event) {
  if (syncing.value) return
  pulse(event)
  syncing.value = true
  syncNote.value = ''
  try {
    const result = await api.syncRun()
    if (result?.error) {
      syncNote.value = result.error
    } else {
      syncNote.value = `已同步，新增 ${result?.inserted ?? 0} 条`
    }
    await loadAccounts()
    await loadAll()
  } catch (e) {
    syncNote.value = String(e.message || e)
  } finally {
    syncing.value = false
    window.setTimeout(() => { syncNote.value = '' }, 5000)
  }
}

function focusSearch(event) {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault()
    searchInput.value?.focus()
  }
}

const drawerPeers = computed(() => {
  const has = new Set(usage.value.map((u) => u.peer_id))
  const rest = peerOptions.value.filter((p) => !has.has(p.peer_id))
  return rest.length ? rest : peerOptions.value
})

onMounted(async () => {
  try {
    version.value = (await api.health()).version
  } catch {
    /* 忽略 */
  }
  window.addEventListener('keydown', focusSearch)
  await loadAccounts()
  await loadAll()
  await loadSyncStatus()
  statusTimer = window.setInterval(loadSyncStatus, 3000)
  await nextTick()
  animateDashboard()
  refreshTriggerTimer = window.setTimeout(() => ScrollTrigger.refresh(), 500)
  refreshTimer = window.setInterval(loadAll, 60_000)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', focusSearch)
  if (refreshTimer) window.clearInterval(refreshTimer)
  if (statusTimer) window.clearInterval(statusTimer)
  if (refreshTriggerTimer) window.clearTimeout(refreshTriggerTimer)
  introCtx?.revert()
})
</script>

<template>
  <div ref="appRoot" class="app-shell">
    <WeuiBackdrop />
    <div v-if="sidebarOpen" class="sidebar-scrim" @click="sidebarOpen = false"></div>

    <aside class="sidebar" :class="{ open: sidebarOpen }">
      <div class="brand-row">
        <span class="logo-mark">W</span>
        <span class="brand-copy">
          <strong>WeQuota</strong>
          <small>微信聊天额度</small>
        </span>
        <button class="weui-btn weui-btn_icon sidebar-close" aria-label="关闭菜单" @click="sidebarOpen = false">
          <Icon name="close" :size="18" />
        </button>
      </div>

      <nav class="side-nav">
        <div class="side-label">工作台</div>
        <button
          v-for="item in primaryNav"
          :key="item.id"
          class="weui-cell weui-cell_access nav-item"
          :class="{ active: activeNav === item.id }"
          @click="go(item.id)"
        >
          <span class="weui-cell__hd"><Icon :name="item.icon" :size="17" /></span>
          <span class="weui-cell__bd">{{ item.label }}</span>
          <span v-if="item.id === 'alerts' && summary.alerts" class="weui-badge nav-badge">
            {{ summary.alerts }}
          </span>
          <span class="weui-cell__ft"></span>
        </button>

        <div class="side-label side-label-gap">管理</div>
        <button
          v-for="item in secondaryNav"
          :key="item.id"
          class="weui-cell weui-cell_access nav-item"
          :class="{ active: activeNav === item.id }"
          @click="go(item.id)"
        >
          <span class="weui-cell__hd"><Icon :name="item.icon" :size="17" /></span>
          <span class="weui-cell__bd">{{ item.label }}</span>
          <span v-if="item.id === 'service'" class="weui-cell__ft nav-status">
            <i class="status-dot" :class="collectorTone"></i>{{ collectorNavText }}
          </span>
          <span v-else class="weui-cell__ft"></span>
        </button>
      </nav>

      <div class="sidebar-user weui-cell">
        <span class="weui-cell__hd"><span class="user-avatar">W</span></span>
        <span class="weui-cell__bd user-copy">
          <strong>本地工作区</strong>
          <small>数据不出本机</small>
        </span>
        <span class="status-dot" :class="collectorTone"></span>
      </div>

      <div class="sidebar-foot">
        <div class="collector-pill">
          <span class="status-dot" :class="collectorTone"></span>
          <span>{{ collectorLabel }}</span>
        </div>
        <div class="sidebar-version">
          <span>v{{ version || '0.1.0' }}</span>
          <span>本地优先</span>
        </div>
      </div>
    </aside>

    <div class="main-shell">
      <header class="topbar">
        <button class="weui-btn weui-btn_icon menu-btn" aria-label="打开菜单" @click="sidebarOpen = true">
          <Icon name="menu" :size="18" />
        </button>

        <div class="crumb">
          <span>工作台</span>
          <Icon name="chevron" :size="14" />
          <strong>{{ activeLabel }}</strong>
        </div>

        <div class="top-actions">
          <div class="weui-search-bar top-search">
            <form class="weui-search-bar__form" @submit.prevent>
              <div class="weui-search-bar__box">
                <i class="weui-icon-search"></i>
                <input
                  ref="searchInput"
                  v-model="query"
                  type="search"
                  class="weui-search-bar__input"
                  placeholder="搜索联系人…"
                  aria-label="搜索联系人"
                />
                <a v-if="query" href="javascript:;" class="weui-icon-clear" aria-label="清除搜索" @click="query = ''"></a>
              </div>
            </form>
          </div>

          <select
            v-if="accounts.length"
            v-model="accountId"
            class="weui-input account-select"
            aria-label="切换微信号"
            @change="switchAccount"
          >
            <option v-for="a in accounts" :key="a.account_id" :value="a.account_id">
              {{ a.display_name || a.account_id }}
            </option>
          </select>

          <button class="weui-btn weui-btn_primary sync-btn" :disabled="syncing" @click="runSync">
            <span v-if="syncing" class="weui-loading"></span>
            <Icon v-else name="refresh" :size="15" />
            <span>{{ syncing ? '同步中' : '同步数据' }}</span>
          </button>

          <button
            class="weui-btn weui-btn_icon theme-btn"
            :title="isDark ? '切换到浅色模式' : '切换到深色模式'"
            :aria-label="isDark ? '切换到浅色模式' : '切换到深色模式'"
            @click="toggleTheme"
          >
            <Icon :name="isDark ? 'sun' : 'moon'" :size="17" />
          </button>

          <button class="avatar-btn" aria-label="账号身份" @click="showIdentity = true">
            <span class="avatar">{{ accountInitial }}</span>
          </button>
        </div>
      </header>

      <main v-if="view === 'dashboard'" class="content">
        <div v-if="error" class="weui-toptips weui-toptips_warn app-toptips">接口异常：{{ error }}</div>

        <section id="overview" class="page-heading">
          <div class="page-title">
            <span class="page-kicker">WECHAT · LOCAL FIRST</span>
            <h1>额度总览</h1>
            <p>实时查看微信聊天 Token 消耗、消息频率与提醒状态。</p>
          </div>
          <div class="heading-meta">
            <button id="account" class="weui-cell weui-cell_access account-summary" @click="showIdentity = true">
              <span class="weui-cell__hd">
                <span class="account-avatar">
                  <img
                    v-if="currentAccount.avatar_url && !avatarFailed"
                    :src="currentAccount.avatar_url"
                    :alt="accountName"
                    @error="avatarFailed = true"
                  />
                  <span v-else>{{ accountInitial }}</span>
                </span>
              </span>
              <span class="weui-cell__bd account-copy">
                <strong>{{ accountName }}</strong>
                <small>微信号 {{ accountWechatId }}</small>
                <small class="account-id">账号 ID {{ currentAccount.account_id }}</small>
              </span>
              <span class="weui-cell__ft account-action">身份</span>
            </button>
            <div class="heading-controls">
              <div class="range-switch">
                <button
                  v-for="r in RANGES"
                  :key="r.key"
                  class="weui-btn weui-btn_mini"
                  :class="days === r.key ? 'weui-btn_primary' : 'weui-btn_default'"
                  @click="days = r.key; loadAll()"
                >
                  {{ r.label }}
                </button>
              </div>
              <span v-if="syncNote" class="sync-note">{{ syncNote }}</span>
            </div>
          </div>
        </section>

        <CollectionProgress />

        <section class="kpi-grid weui-grids">
          <div v-for="k in kpis" :key="k.label" class="weui-grid kpi-card">
            <span class="weui-grid__label kpi-label">{{ k.label }}</span>
            <CountUp v-if="k.numeric" :value="k.number" class="kpi-number" />
            <b v-else class="num kpi-number compact">{{ k.value }}</b>
            <span class="kpi-hint">{{ k.hint }}</span>
          </div>
        </section>

        <section class="hero-stack">
          <div id="timeseries" class="hero-chart">
            <TimeSeriesPanel :peer-id="selectedPeer" :peer-name="selectedName" />
          </div>
          <div id="frequency" class="hero-heatmap">
            <MessageHeatmap :account-id="accountId" />
          </div>
        </section>

        <section class="dashboard-grid">
          <div id="quota" class="span-6">
            <QuotaCards :items="filteredUsage" @edit="openEdit" @add="openAdd" />
          </div>
          <div id="metrics" class="span-3">
            <KeyMetricsPanel :summary="summary" :usage="usage" :collector="collector" />
          </div>
          <div id="alerts" class="span-3">
            <AlertPanel />
          </div>

          <div id="ranking" class="span-7">
            <RankingBoard :items="filteredRanking" @click="selectPeer" />
          </div>
          <div id="types" class="span-5">
            <TypePie :items="types" />
          </div>

          <div id="report" class="span-7">
            <ReportPanel :peer-id="selectedPeer" />
          </div>
          <div id="peers" class="span-5">
            <div class="weui-panel peer-picker">
              <div class="weui-panel__hd">
                <div>
                  <strong>联系人筛选</strong>
                  <span class="panel-sub">全部联系人 · 可滚动选择</span>
                </div>
                <span class="weui-badge weui-badge_plain">{{ filteredPeers.length }} 人</span>
              </div>
              <div class="weui-panel__bd">
                <button
                  class="weui-btn weui-btn_mini"
                  :class="selectedPeer === '' ? 'weui-btn_primary' : 'weui-btn_default'"
                  @click="clearPeer"
                >
                  全部联系人
                </button>
                <div v-if="!filteredPeers.length" class="empty">
                  {{ query ? '没有匹配的联系人' : '暂无联系人数据' }}
                </div>
                <div v-else ref="peerListEl" class="peer-list" @scroll="onPeerScroll">
                  <div class="peer-list-inner" :style="{ height: `${peerTotalHeight}px` }">
                    <button
                      v-for="p in peerWindowItems"
                      :key="p.peer_id"
                      class="peer-item"
                      :class="{ active: selectedPeer === p.peer_id }"
                      :style="{ top: `${p._top}px` }"
                      @click="selectPeer(p)"
                    >
                      <span class="peer-avatar">{{ (p.peer_name || p.peer_id).slice(0, 1).toUpperCase() }}</span>
                      <span class="peer-name">{{ p.peer_name || p.peer_id }}</span>
                      <span class="peer-count num">{{ p.messages }} 条</span>
                    </button>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </section>
      </main>

      <main v-else class="content settings-content">
        <SettingsPanel
          page
          :open="view === 'settings'"
          @close="closeSettings"
          @saved="loadAll"
        />
      </main>
    </div>

    <QuotaDialog
      :open="drawerOpen"
      :item="editing"
      :peers="drawerPeers"
      @close="drawerOpen = false"
      @saved="onQuotaSaved"
    />

    <div v-if="showIdentity" class="app-dialog-mask" @click.self="showIdentity = false">
      <div ref="identityDialog" class="weui-dialog identity-dialog">
        <div class="weui-dialog__hd">
          <strong class="weui-dialog__title">账号身份</strong>
        </div>
        <div class="weui-dialog__bd">
          <div class="identity-head">
            <span class="account-avatar large">
              <img
                v-if="currentAccount.avatar_url && !avatarFailed"
                :src="currentAccount.avatar_url"
                :alt="accountName"
                @error="avatarFailed = true"
              />
              <span v-else>{{ accountInitial }}</span>
            </span>
            <div>
              <h3>{{ accountName }}</h3>
              <p>微信号 {{ accountWechatId }}</p>
              <p>账号 ID {{ currentAccount.account_id }}</p>
            </div>
          </div>

          <div class="weui-cells identity-cells">
            <div class="weui-cell">
              <div class="weui-cell__bd">稳定身份</div>
              <div class="weui-cell__ft identity-code">{{ currentAccount.stable_id || '尚未检测到' }}</div>
            </div>
          </div>

          <p class="identity-note">
            微信号可以修改，但本机微信数据目录中的 wxid 通常是稳定的。WeQuota 优先用这个稳定身份识别同一份聊天记录；
            检测到同身份别名后，可在此确认合并。合并只会在你点击按钮后执行。
          </p>

          <div v-if="accountAliases.length > 1" class="identity-aliases">
            <p>检测到 {{ accountAliases.length }} 个同身份账号，合并目标：</p>
            <strong>{{ mergeTarget?.display_name || mergeTarget?.account_id }}</strong>
            <div class="identity-alias-list">
              <span v-for="a in accountAliases" :key="a.account_id" class="weui-badge weui-badge_plain">
                {{ a.account_id }}{{ a.account_id === mergeTarget?.account_id ? '（最新）' : '' }}
              </span>
            </div>
          </div>

          <div v-if="accounts.length > 1" class="identity-manual weui-cells weui-cells_checkbox">
            <div class="identity-manual-head">
              <strong>手动选择同一账号的别名</strong>
              <small>选择两个或更多，合并到其中最新活跃的账号</small>
            </div>
            <label v-for="a in accounts" :key="a.account_id" class="weui-cell weui-check__label identity-check">
              <span class="weui-cell__hd">
                <input v-model="selectedAccounts" class="weui-check" type="checkbox" :value="a.account_id" />
                <i class="weui-icon-checked"></i>
              </span>
              <span class="weui-cell__bd">
                <strong>{{ a.display_name || a.account_id }}</strong>
                <small>{{ a.account_id }}</small>
              </span>
              <span class="weui-cell__ft">{{ a.messages }} 条</span>
            </label>
            <button
              class="weui-btn weui-btn_primary weui-btn_block identity-merge-btn"
              :disabled="selectedAccounts.length < 2 || merging"
              @click="mergeSelected"
            >
              {{ merging ? '合并中…' : '合并选中账号' }}
            </button>
          </div>

          <p v-if="mergeError" class="identity-error">{{ mergeError }}</p>

          <div class="identity-actions">
            <button class="weui-btn weui-btn_default" @click="showIdentity = false">关闭</button>
            <button
              v-if="accountAliases.length > 1"
              class="weui-btn weui-btn_primary"
              :disabled="merging"
              @click="mergeAliases"
            >
              {{ merging ? '合并中…' : '合并到最新账号' }}
            </button>
          </div>
        </div>
      </div>
    </div>

    <div v-if="needToken" class="app-dialog-mask">
      <div class="weui-dialog auth-dialog">
        <div class="weui-dialog__hd">
          <strong class="weui-dialog__title">需要访问令牌</strong>
        </div>
        <div class="weui-dialog__bd">
          <p class="auth-copy">服务端已启用鉴权。请输入 <code>config.json</code> → <code>app.auth_token</code> 配置的令牌。</p>
          <div class="weui-cells">
            <div class="weui-cell">
              <div class="weui-cell__bd">
                <input
                  v-model="tokenInput"
                  type="password"
                  class="weui-input"
                  placeholder="访问令牌"
                  autocomplete="current-password"
                  @keyup.enter="submitToken"
                />
              </div>
            </div>
          </div>
          <p v-if="tokenError" class="auth-err">{{ tokenError }}</p>
          <button class="weui-btn weui-btn_primary weui-btn_block auth-submit" @click="submitToken">确定</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.app-shell {
  position: relative;
  isolation: isolate;
  display: flex;
  min-height: 100vh;
  background: var(--bg);
}

/* ---------- sidebar ---------- */
.sidebar {
  position: fixed;
  inset: 0 auto 0 0;
  z-index: 30;
  display: flex;
  flex-direction: column;
  width: 248px;
  height: 100vh;
  padding: 22px 14px 16px;
  color: var(--sidebar-text);
  background:
    radial-gradient(circle at 18% 0%, color-mix(in srgb, var(--accent) 12%, transparent), transparent 38%),
    linear-gradient(180deg, var(--sidebar-2), var(--sidebar));
  border-right: 1px solid var(--sidebar-border);
}
.brand-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 4px 8px 22px;
}
.logo-mark {
  display: grid;
  place-items: center;
  width: 36px;
  height: 36px;
  flex: 0 0 36px;
  border-radius: 11px;
  background: var(--accent);
  color: var(--accent-ink);
  font-size: 18px;
  font-weight: 800;
  letter-spacing: -0.04em;
  box-shadow: 0 0 0 5px color-mix(in srgb, var(--accent) 10%, transparent);
}
.brand-copy {
  display: flex;
  flex-direction: column;
  min-width: 0;
  line-height: 1.15;
}
.brand-copy strong { color: var(--sidebar-text); font-size: 17px; font-weight: 700; }
.brand-copy small { margin-top: 4px; color: var(--sidebar-muted); font-size: 10px; }
.sidebar-close { display: none; margin-left: auto; border-color: var(--sidebar-border); background: transparent; color: var(--sidebar-muted); }

.side-nav {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-height: 0;
  overflow-y: auto;
}
.side-label {
  padding: 11px 10px 6px;
  color: var(--sidebar-muted);
  font-size: 10px;
  font-weight: 500;
}
.side-label-gap { margin-top: 12px; }
.side-nav .nav-item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 11px;
  width: 100%;
  padding: 9px 10px;
  border: 1px solid transparent;
  border-radius: 10px;
  background: transparent;
  color: var(--sidebar-muted);
  font-size: 13px;
  line-height: 1.2;
  text-align: left;
  white-space: nowrap;
  transition: color 0.15s ease, background-color 0.15s ease, border-color 0.15s ease;
}
.side-nav .nav-item::before { display: none; content: none; }
.side-nav .nav-item:hover {
  color: var(--sidebar-text);
  background: color-mix(in srgb, var(--sidebar-text) 5%, transparent);
}
.side-nav .nav-item.active {
  color: var(--sidebar-text);
  background: color-mix(in srgb, var(--accent) 14%, transparent);
  border-color: color-mix(in srgb, var(--accent) 18%, transparent);
}
.side-nav .nav-item.active .weui-cell__hd { color: var(--accent); }
.side-nav .nav-item .weui-cell__hd { display: grid; place-items: center; color: var(--sidebar-muted); }
.side-nav .nav-item .weui-cell__bd { min-width: 0; }
.side-nav .nav-item .weui-cell__ft { display: none; }
.side-nav .nav-item .weui-cell__ft.nav-status {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  margin-left: auto;
  color: var(--sidebar-muted);
  font-size: 9px;
  white-space: nowrap;
}
.side-nav .nav-item .weui-cell__ft.nav-status .status-dot { width: 5px; height: 5px; flex-basis: 5px; }
.nav-status .status-dot.ok { background: var(--accent); box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 15%, transparent); }
.nav-status .status-dot.warn { background: var(--warn); }
.nav-status .status-dot.danger { background: var(--danger); }
.nav-badge { margin-left: auto; }

.sidebar-user {
  display: flex;
  align-items: center;
  gap: 9px;
  margin-top: auto;
  padding: 10px 9px;
  border: 1px solid var(--sidebar-border);
  border-radius: 11px;
  background: color-mix(in srgb, var(--sidebar-text) 4%, transparent);
}
.sidebar-user::before { display: none; }
.user-avatar {
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  border-radius: 8px;
  background: var(--accent);
  color: var(--accent-ink);
  font-size: 11px;
  font-weight: 800;
}
.user-copy { display: flex; flex-direction: column; min-width: 0; line-height: 1.25; }
.user-copy strong { color: var(--sidebar-text); font-size: 11px; font-weight: 600; }
.user-copy small { color: var(--sidebar-muted); font-size: 9px; margin-top: 2px; }
.sidebar-user .status-dot { margin-left: auto; }

.sidebar-foot {
  padding: 10px 8px 4px;
  margin-top: 10px;
  border-top: 1px solid var(--sidebar-border);
}
.collector-pill {
  display: flex;
  align-items: center;
  gap: 7px;
  color: var(--sidebar-muted);
  font-size: 10px;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.status-dot {
  width: 7px;
  height: 7px;
  flex: 0 0 7px;
  border-radius: 50%;
  background: var(--sidebar-muted);
}
.status-dot.ok { background: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 13%, transparent); }
.status-dot.warn { background: var(--warn); }
.status-dot.danger { background: var(--danger); }
.status-dot.off { background: var(--sidebar-muted); }
.sidebar-version { display: flex; gap: 8px; margin-top: 8px; color: var(--sidebar-muted); font-size: 9px; }

/* ---------- main / topbar ---------- */
.main-shell {
  position: relative;
  z-index: 1;
  display: flex;
  flex: 1;
  min-width: 0;
  margin-left: 248px;
  flex-direction: column;
}
.topbar {
  position: sticky;
  top: 0;
  z-index: 20;
  display: flex;
  align-items: center;
  gap: 18px;
  min-height: 68px;
  padding: 0 28px;
  background: color-mix(in srgb, var(--surface) 88%, transparent);
  border-bottom: 1px solid var(--border);
  backdrop-filter: blur(18px);
}
.menu-btn { display: none; }
.crumb { display: flex; align-items: center; gap: 6px; color: var(--faint); font-size: 12px; white-space: nowrap; }
.crumb strong { color: var(--text); font-weight: 600; }
.top-actions { display: flex; align-items: center; gap: 8px; margin-left: auto; min-width: 0; }
.top-search { width: min(300px, 24vw); padding: 0; background: transparent; }
.top-search .weui-search-bar__form { height: 34px; }
.top-search .weui-search-bar__box { height: 100%; }
.account-select {
  max-width: 150px;
  height: 34px;
  padding: 0 10px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface);
  color: var(--text);
  font-size: 11px;
}
.sync-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 34px;
  padding: 0 14px;
  line-height: 32px;
  white-space: nowrap;
}
.sync-btn .weui-loading { width: 14px; height: 14px; margin: 0; }
.theme-btn { flex: 0 0 34px; width: 34px; height: 34px; }
.avatar-btn {
  display: grid;
  place-items: center;
  width: 34px;
  height: 34px;
  flex: 0 0 34px;
  padding: 0;
  border: 0;
  border-radius: 10px;
  background: transparent;
}
.avatar {
  display: grid;
  place-items: center;
  width: 34px;
  height: 34px;
  border-radius: 10px;
  background: var(--text);
  color: var(--surface);
  font-size: 12px;
  font-weight: 700;
}

.content {
  width: 100%;
  max-width: 1500px;
  margin: 0 auto;
  padding: 28px 28px 48px;
}
.content [id] { scroll-margin-top: 88px; }
.settings-content { max-width: 1180px; }
.app-toptips {
  display: block;
  position: static;
  width: 100%;
  margin-bottom: 16px;
  border-radius: 10px;
  text-align: left;
}

/* ---------- page heading ---------- */
.page-heading {
  position: relative;
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 20px;
  isolation: isolate;
}
.page-heading::before {
  position: absolute;
  z-index: -1;
  top: -80px;
  left: -60px;
  width: 480px;
  height: 240px;
  content: '';
  pointer-events: none;
  background: radial-gradient(ellipse, color-mix(in srgb, var(--accent) 10%, transparent), transparent 70%);
}
.page-kicker {
  display: block;
  margin-bottom: 7px;
  color: var(--accent-strong);
  font-size: 10px;
  font-weight: 650;
  letter-spacing: 0.12em;
}
.page-title h1 {
  color: var(--text);
  font-size: clamp(32px, 4vw, 46px);
  font-weight: 650;
  line-height: 1.04;
  letter-spacing: -0.055em;
}
.page-title p { margin: 8px 0 0; color: var(--muted); font-size: 12px; line-height: 1.6; }
.heading-meta { display: flex; flex-direction: column; align-items: flex-end; gap: 10px; }
.heading-controls { display: flex; align-items: center; gap: 10px; }
.range-switch { display: flex; gap: 6px; }
.sync-note { color: var(--accent-strong); font-size: 11px; }
.account-summary {
  display: flex;
  align-items: center;
  gap: 9px;
  min-width: 230px;
  padding: 7px 9px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--text);
  text-align: left;
  white-space: nowrap;
  box-shadow: var(--shadow-soft);
}
.account-summary::before { display: none; }
.account-summary:hover { border-color: var(--border-strong); }
.account-avatar {
  display: grid;
  place-items: center;
  width: 38px;
  height: 38px;
  overflow: hidden;
  border-radius: 11px;
  background: var(--accent-soft);
  color: var(--accent-strong);
  font-size: 15px;
  font-weight: 750;
}
.account-avatar.large { width: 46px; height: 46px; border-radius: 13px; font-size: 18px; }
.account-avatar img { width: 100%; height: 100%; object-fit: cover; }
.account-copy { display: flex; flex-direction: column; min-width: 0; line-height: 1.25; }
.account-copy strong { color: var(--text); font-size: 12px; font-weight: 650; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.account-copy small { color: var(--muted); font-size: 10px; white-space: nowrap; }
.account-copy .account-id { color: var(--faint); }
.account-action { color: var(--accent-strong); font-size: 10px; white-space: nowrap; }

/* ---------- KPI ---------- */
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 18px;
}
.weui-grid.kpi-card {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  width: auto;
  min-width: 0;
  padding: 15px 16px;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  text-align: left;
  box-shadow: var(--shadow-soft);
  transition: border-color 0.18s ease, transform 0.18s ease;
}
.weui-grid.kpi-card::after {
  position: absolute;
  right: -70px;
  bottom: -90px;
  width: 190px;
  height: 190px;
  content: '';
  pointer-events: none;
  opacity: 0;
  background: radial-gradient(circle, color-mix(in srgb, var(--accent) 13%, transparent), transparent 68%);
  transition: opacity 0.2s ease;
}
.weui-grid.kpi-card:hover { border-color: var(--border-strong); transform: translateY(-2px); }
.weui-grid.kpi-card:hover::after { opacity: 1; }
.kpi-label { color: var(--muted); font-size: 11px; white-space: nowrap; }
.kpi-number {
  display: block;
  margin: 3px 0 1px;
  color: var(--text);
  font-size: 24px;
  font-weight: 700;
  line-height: 1.2;
  letter-spacing: -0.03em;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.kpi-number.compact { font-size: 17px; }
.kpi-hint { color: var(--faint); font-size: 9px; }

/* ---------- grids ---------- */
.hero-stack { display: flex; flex-direction: column; gap: 18px; margin-bottom: 18px; }
.hero-chart, .hero-heatmap { position: relative; min-width: 0; }
.hero-chart > :deep(.weui-panel), .hero-heatmap > :deep(.weui-panel) { height: 100%; }
.dashboard-grid {
  display: grid;
  grid-template-columns: repeat(12, minmax(0, 1fr));
  gap: 18px;
  align-items: stretch;
}
.dashboard-grid > div { min-width: 0; }
.dashboard-grid > div > :deep(.weui-panel) { height: 100%; }
.span-3 { grid-column: span 3; }
.span-5 { grid-column: span 5; }
.span-6 { grid-column: span 6; }
.span-7 { grid-column: span 7; }
.peer-picker { margin: 0; }
.peer-chips { display: flex; flex-wrap: wrap; gap: 7px; }
.peer-chips .weui-btn { max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.peer-list {
  position: relative;
  height: 330px;
  margin-top: 10px;
  overflow-y: auto;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface-2);
}
.peer-list-inner { position: relative; }
.peer-item {
  position: absolute;
  left: 0;
  right: 0;
  display: flex;
  align-items: center;
  gap: 9px;
  width: 100%;
  height: 46px;
  padding: 0 10px;
  border: 0;
  border-bottom: 1px solid var(--border);
  background: transparent;
  color: var(--text);
  text-align: left;
}
.peer-item:hover { background: var(--surface-3); }
.peer-item.active {
  background: var(--accent-soft);
  color: var(--accent-strong);
}
.peer-avatar {
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  flex: 0 0 24px;
  border-radius: 7px;
  background: var(--surface);
  color: var(--muted);
  font-size: 10px;
  font-weight: 700;
}
.peer-item.active .peer-avatar { background: var(--accent); color: var(--accent-ink); }
.peer-name {
  min-width: 0;
  flex: 1;
  overflow: hidden;
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.peer-count { color: var(--faint); font-size: 10px; white-space: nowrap; }

/* ---------- identity / auth dialogs ---------- */
.app-dialog-mask {
  position: fixed;
  inset: 0;
  z-index: 60;
  display: grid;
  place-items: center;
  padding: 20px;
  background: color-mix(in srgb, var(--sidebar) 56%, transparent);
  backdrop-filter: blur(8px);
}
.identity-dialog { width: min(580px, 100%); max-height: calc(100vh - 40px); display: flex; flex-direction: column; text-align: left; }
.identity-dialog .weui-dialog__bd { overflow-y: auto; }
.identity-head { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
.identity-head h3 { margin-bottom: 4px; font-size: 18px; }
.identity-head p { margin: 2px 0; color: var(--muted); font-size: 11px; }
.identity-cells { margin-bottom: 12px; }
.identity-code { max-width: 60%; overflow-wrap: anywhere; text-align: right; }
.identity-note { margin: 12px 0; color: var(--muted); font-size: 11px; line-height: 1.7; }
.identity-aliases { margin-bottom: 14px; color: var(--muted); font-size: 11px; }
.identity-aliases p { margin: 0 0 4px; }
.identity-aliases strong { color: var(--text); font-size: 12px; }
.identity-alias-list { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.identity-manual {
  margin: 14px 0;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface-2);
}
.identity-manual-head { display: flex; flex-direction: column; gap: 3px; margin-bottom: 4px; }
.identity-manual-head strong { color: var(--text); font-size: 12px; }
.identity-manual-head small { color: var(--muted); font-size: 10px; }
.identity-check { padding: 8px 0; }
.identity-check::before { border-top-color: var(--border); }
.identity-check .weui-cell__bd { display: flex; flex-direction: column; }
.identity-check .weui-cell__bd strong { color: var(--text); font-size: 11px; }
.identity-check .weui-cell__bd small { color: var(--faint); font-size: 10px; }
.identity-check .weui-check:checked + .weui-icon-checked { color: var(--accent); }
.identity-merge-btn { margin-top: 10px; }
.identity-error { color: var(--danger); font-size: 11px; }
.identity-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
.auth-dialog { width: min(420px, 100%); text-align: left; }
.auth-copy { margin: 0 0 12px; color: var(--muted); font-size: 12px; line-height: 1.7; }
.auth-copy code { padding: 1px 5px; border-radius: 4px; background: var(--surface-2); }
.auth-err { margin: 8px 0 0; color: var(--danger); font-size: 11px; }
.auth-submit { margin-top: 14px; }
.sidebar-scrim {
  position: fixed;
  inset: 0;
  z-index: 29;
  background: color-mix(in srgb, var(--sidebar) 48%, transparent);
}

/* ---------- responsive ---------- */
@media (max-width: 1260px) {
  .kpi-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .span-3 { grid-column: span 6; }
  .span-5, .span-7 { grid-column: span 6; }
  .span-6 { grid-column: span 12; }
}
@media (max-width: 1080px) {
  .sidebar {
    left: 0;
    top: 0;
    transform: translateX(-102%);
    transition: transform 0.2s ease;
    box-shadow: 18px 0 50px color-mix(in srgb, var(--sidebar) 24%, transparent);
  }
  .sidebar.open { transform: translateX(0); }
  .main-shell { margin-left: 0; }
  .sidebar-close { display: inline-grid; place-items: center; }
  .menu-btn { display: inline-grid; }
  .content { padding: 24px 22px 40px; }
  .topbar { padding: 0 22px; }
}
@media (max-width: 820px) {
  .top-search { width: 190px; }
  .account-select { display: none; }
  .crumb span, .crumb svg { display: none; }
  .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .span-3, .span-5, .span-6, .span-7 { grid-column: span 12; }
  .page-heading { align-items: flex-start; flex-direction: column; }
  .heading-meta { align-items: flex-start; width: 100%; }
  .heading-controls { flex-wrap: wrap; }
  .account-summary { width: 100%; }
}
@media (max-width: 560px) {
  .topbar { min-height: 62px; padding: 0 14px; gap: 8px; }
  .content { padding: 20px 14px 32px; }
  .top-search { width: 100%; }
  .top-actions { gap: 6px; }
  .sync-btn span:last-child { display: none; }
  .kpi-grid { grid-template-columns: 1fr; }
  .page-title h1 { font-size: 28px; }
  .side-nav { overflow: visible; }
}
</style>
