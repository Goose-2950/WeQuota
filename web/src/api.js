/** 统一的数据访问层：同源调用 FastAPI（前端由 FastAPI 静态托管）。 */
const base = ''
const TOKEN_KEY = 'wequota_token'

/** 访问令牌：配置了 app.auth_token 后由大屏提示输入，存本地。 */
export const auth = {
  get: () => {
    try {
      return localStorage.getItem(TOKEN_KEY) || ''
    } catch {
      return ''
    }
  },
  set: (token) => {
    try {
      const value = String(token || '').trim()
      if (value) localStorage.setItem(TOKEN_KEY, value)
      else localStorage.removeItem(TOKEN_KEY)
    } catch {
      /* 隐私模式等无法写入时忽略，本次会话仍可手动重试 */
    }
  }
}

/** 当前查看的微信号（多账号隔离）；由 App.vue 的账号切换器写入。 */
let currentAccount = ''

class HttpError extends Error {
  constructor(message, status) {
    super(message)
    this.name = 'HttpError'
    this.status = status
  }
}

function authHeaders(extra = {}) {
  const token = auth.get()
  return token ? { ...extra, Authorization: `Bearer ${token}` } : extra
}

export const account = {
  get: () => currentAccount,
  set: (id) => {
    currentAccount = id || ''
  }
}

function withAccount(url) {
  if (!currentAccount) return url
  return url + (url.includes('?') ? '&' : '?') + 'account_id=' + encodeURIComponent(currentAccount)
}

async function getJSON(url) {
  const res = await fetch(base + withAccount(url), { headers: authHeaders() })
  if (!res.ok) throw new HttpError(`${url} → HTTP ${res.status}`, res.status)
  return res.json()
}

async function sendJSON(url, method, body) {
  const res = await fetch(base + url, {
    method,
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: body === undefined ? undefined : JSON.stringify(body)
  })
  if (!res.ok) throw new HttpError(`${url} → HTTP ${res.status}`, res.status)
  return res.json()
}

async function postJSON(url, body) {
  return sendJSON(url, 'POST', body)
}

export const api = {
  health: () => getJSON('/api/health'),

  // 多微信号 / 采集器（M7）
  accounts: () => getJSON('/api/accounts'),
  mergeAccounts: (payload) => postJSON('/api/accounts/merge', {
    confirm: true,
    ...payload
  }),
  collectorStatus: () => getJSON('/api/collector/status'),
  syncStatus: () => getJSON('/api/sync/status'),
  syncRun: () => postJSON('/api/sync/run'),

  usage: () => getJSON('/api/quota/usage'),
  rules: () => getJSON('/api/quota/rules'),
  setRule: (rule) => postJSON('/api/quota/rules', {
    ...rule,
    account_id: rule.account_id || currentAccount || undefined
  }),
  deleteRule: (peerId) => sendJSON(`/api/quota/rules/${encodeURIComponent(peerId)}`, 'DELETE'),
  peers: () => getJSON('/api/peers'),

  // M11 大屏设置
  settings: () => getJSON('/api/settings'),
  saveSettings: (values) => sendJSON('/api/settings', 'PUT', { values }),
  testEmail: (values) => postJSON('/api/settings/test-email', values ? { values } : {}),

  transcribeStatus: () => getJSON('/api/transcribe/status'),
  downloadModel: () => postJSON('/api/transcribe/download'),

  summary: (days = 7) => getJSON(`/api/stats/summary?days=${days}`),
  ranking: (days = 7, limit = 20) => getJSON(`/api/stats/ranking?days=${days}&limit=${limit}`),
  timeseries: (bucket = 'hour', days = 1, peerId = '') =>
    getJSON(`/api/stats/timeseries?bucket=${bucket}&days=${days}${peerId ? `&peer_id=${peerId}` : ''}`),
  heatmap: (year = '') =>
    getJSON(`/api/stats/heatmap${year ? `?year=${year}` : ''}`),
  byType: (days = 7, peerId = '') =>
    getJSON(`/api/stats/by-type?days=${days}${peerId ? `&peer_id=${peerId}` : ''}`),
  report: (period = 'week', peerId = '') =>
    getJSON(`/api/stats/report?period=${period}${peerId ? `&peer_id=${peerId}` : ''}`),

  alerts: (limit = 30) => getJSON(`/api/alerts?limit=${limit}`),
  alertStatus: () => getJSON('/api/alerts/status')
}

/** 连接告警 WebSocket：连上先收到历史回放，之后实时推送。 */
export function connectAlerts(onMessage, onStatus) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  let ws = null
  let closed = false
  let retry = 0

  function open() {
    if (closed) return
    const token = auth.get()
    const query = token ? `?token=${encodeURIComponent(token)}` : ''
    ws = new WebSocket(`${proto}://${location.host}/ws/alerts${query}`)
    ws.onopen = () => {
      retry = 0
      onStatus?.('connected')
    }
    ws.onmessage = (e) => {
      try {
        onMessage?.(JSON.parse(e.data))
      } catch {
        /* 忽略非法报文 */
      }
    }
    ws.onclose = () => {
      onStatus?.('disconnected')
      if (closed) return
      retry = Math.min(retry + 1, 6)
      setTimeout(open, 1000 * retry)
    }
    ws.onerror = () => ws?.close()
  }

  open()
  return () => {
    closed = true
    ws?.close()
  }
}
