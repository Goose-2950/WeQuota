# 前端组件库与动效方案

更新：2026-11-10。数据大屏已从 Vuetify / Naive UI / Inspira UI 切换为
**WeUI 原生组件 + GSAP**，目标是让界面回到微信原生的语言，同时用 GSAP
完成高级动效和交互。

## 组件库：WeUI

选择 **`weui`**（`weui/weui`，2.6.26，MIT）：

```bash
cd web
npm install weui gsap
```

入口：`web/src/main.js` 引入 `weui/dist/style/weui.css`，再加载
`web/src/styles.css` 做项目和深色主题覆盖。

### WeUI 原生组件使用位置

| WeUI 组件 | 用途 | 位置 |
|------|------|------|
| `weui-panel` / `weui-panel__hd` / `weui-panel__bd` | 所有数据卡片 | 各面板组件 |
| `weui-cells` / `weui-cell` | 额度列表、告警列表、设置项、账号身份 | 各组件 |
| `weui-btn` / `weui-btn_primary` / `weui-btn_mini` | 同步、筛选、保存、返回 | App / 各面板 |
| `weui-search-bar` | 联系人搜索 | `App.vue` |
| `weui-progress` | 采集进度、额度进度、关键指标 | 进度与指标组件 |
| `weui-badge` | 状态、年份、告警等级 | 各面板 |
| `weui-navbar` | 设置页分组切换 | `SettingsPanel.vue` |
| `weui-dialog` / `weui-mask` | 额度弹窗、账号身份、访问令牌 | `QuotaDialog.vue` / `App.vue` |
| `weui-switch` / `weui-input` | 设置和额度表单 | 设置 / 额度弹窗 |
| `weui-toptips` | 错误提示和实时告警 toast | `App.vue` / `AlertPanel.vue` |
| `weui-grids` / `weui-grid` | 顶部 KPI | `App.vue` |
| `weui-loading` | 同步按钮 loading | `App.vue` |

WeUI 的 `data-weui-theme="dark"` 与项目 `data-theme` 同步，由
`web/src/theme.js` 写入 `<body>`。项目再覆盖 `--weui-BRAND-*`、`--weui-BG-*`、
`--weui-FG-*` 等变量，保证和原绿色视觉一致。

## GSAP 动效

`web/src/motion.js` 统一注册 `ScrollTrigger` 与 `ScrollToPlugin`。

| 动效 | 实现 |
|------|------|
| 首屏标题 / KPI / 面板入场 | `gsap.from` + stagger |
| 下方数据面板滚动揭示 | `ScrollTrigger.batch` |
| KPI 数字滚动 | `CountUp.vue` 中 `gsap.to` 数字对象 |
| 采集进度条 | `gsap.to(bar, { width })` |
| 额度 / 关键指标进度条 | `scaleX` 从 0 到 1 |
| 热力图方块 | 年份切换时 365 格 stagger 出现 |
| 额度弹窗 | `back.out` 弹入 + 退出缩放 |
| 账号身份弹窗 | 缩放 / 位移弹入 |
| 实时告警 toast | 右侧滑入、滑出 |
| 侧边栏导航 | 入场位移 + stagger |
| 背景光斑 | `WeuiBackdrop.vue` 无限 yoyo 漂移 |
| 页面内平滑定位 | `ScrollToPlugin` |

`prefers-reduced-motion: reduce` 下跳过 GSAP 时间线并关闭背景漂移。

## Hallmark 设计参考

保留 `.opencode/skills/hallmark/`（Nutlope/hallmark，MIT）作为设计参考，但本轮以
WeUI 原生组件和微信视觉语言优先；仍保留 `focus-visible`、统一 token、减少无意义装饰
等基础规则。

## 本次重设计落点

- 绿色 / 白色浅色主题 + 深色模式
- WeUI 原生列表、面板、按钮、搜索、进度、弹窗、开关、提示
- 左侧导航与顶部命令栏
- 时段消耗 + 年度消息频率热力图占满整行
- 额度点击后使用 WeUI dialog 弹出卡片
- 设置页使用 WeUI navbar / cells 分组
- 账号头像、微信名、微信号、账号 ID 与稳定身份
- GSAP 负责入场、滚动揭示、数字、进度、弹窗、toast、背景
- 不再依赖 Vuetify / Naive UI / Inspira UI
