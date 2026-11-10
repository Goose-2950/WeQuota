import { computed, ref } from 'vue'

const STORAGE_KEY = 'wequota_theme'
const VALID = new Set(['light', 'dark'])

function initialTheme() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (VALID.has(saved)) return saved
  } catch {
    /* localStorage 不可用时保持默认 */
  }
  return 'light'
}

export const theme = ref(initialTheme())
export const isDark = computed(() => theme.value === 'dark')

export function applyTheme(value = theme.value) {
  if (typeof document !== 'undefined') {
    document.documentElement.dataset.theme = value
    // WeUI 自带 data-weui-theme 深色变量，和项目主题同步
    if (document.body) document.body.dataset.weuiTheme = value
  }
  try {
    localStorage.setItem(STORAGE_KEY, value)
  } catch {
    /* 隐私模式等无法写入时忽略 */
  }
}

export function setTheme(value) {
  theme.value = VALID.has(value) ? value : 'light'
  applyTheme()
}

export function toggleTheme() {
  setTheme(isDark.value ? 'light' : 'dark')
}

/** ECharts 不能直接读 CSS 变量，这里提供一套随主题切换的图表色。 */
export const chartColors = computed(() => (
  isDark.value
    ? {
        text: '#e9f5ee',
        muted: '#8ba596',
        axis: '#2a3b31',
        grid: '#1e2d24',
        surface: '#0f1712',
        surface2: '#142019',
        tooltip: '#0b130e',
        border: '#2a3b31',
        accent: '#23d47a',
        accent2: '#14b866',
        accentSoft: 'rgba(35, 212, 122, .25)',
        ok: '#22c55e',
        warn: '#fbbf24',
        danger: '#f87171',
        purple: '#a78bfa',
        pie: ['#23d47a', '#14b866', '#5ee59b', '#fbbf24', '#f87171', '#38bdf8', '#a78bfa', '#8ba596']
      }
    : {
        text: '#122019',
        muted: '#6b7f73',
        axis: '#d6e1da',
        grid: '#edf2ef',
        surface: '#ffffff',
        surface2: '#f6f8f7',
        tooltip: '#122019',
        border: '#e1e9e4',
        accent: '#07c160',
        accent2: '#06ad56',
        accentSoft: 'rgba(7, 193, 96, .18)',
        ok: '#07c160',
        warn: '#fa9d3b',
        danger: '#fa5151',
        purple: '#7c3aed',
        pie: ['#07c160', '#06ad56', '#5ddb9a', '#fa9d3b', '#fa5151', '#10aeff', '#8b5cf6', '#94a3b8']
      }
))

applyTheme()
