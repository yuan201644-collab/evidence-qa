# 硬编码色值 → 语义 token 映射表

> 任务性质：**纯只读分析**，未修改任何文件。
> 基线：`git HEAD`（工作区正被并行的主题化改造占用，行号以 **HEAD 基线**为准，与任务给出的 155 处逐文件计数完全吻合）。
> 统计口径：`web/src` 下所有 `.vue` / `.css` / `.ts`，**排除 `styles/tokens.css` 自身的变量定义**。
> 基线合计 **158** 处 = 任务清单 10 个文件的 155 处 + `styles/global.css` 的 3 处（任务描述漏列了 global.css）。

---

## 0. 先说三个改变结论的发现（务必先读）

### 发现 1：任务前提已过期——token 层已被完整重建，且是 172 行不是 20 行

任务描述说 `tokens.css` 只有 20 个 `:root` 变量。**当前工作区的 `tokens.css` 已是 172 行**，包含完整的亮/暗两套定义：

- `:root`（亮色）第 18–95 行，`:root[data-theme='dark']`（暗色）第 97–172 行。
- 新增的 token 已经覆盖了本报告原本要"建议新增"的绝大部分名字，且命名与语义分层已经按"表面层级 / 文字 / 边框 / 品牌 / 状态 / 气泡 / 证据 / 遮罩阴影"分好组。

这意味着：**本任务的产物不应是"从零设计建议 token 清单"，而是"把剩余硬编码值对齐到已存在的 token 命名体系"**。我在第 2 节给出的是**对照表**：已存在可直接用 / 需新增。

### 发现 2：暗色模式的基础设施已经建好了

以下文件均已被修改（`git status` 显示 M，时间戳 2026/10/12 01:01–01:02，即**本次会话期间由并行 agent 写入**）：

| 文件 | 状态 |
|---|---|
| `web/src/styles/tokens.css` | 已重写为双层 172 行，含暗色全套 |
| `web/src/utils/theme.ts` | **新增文件**（92 行）：`data-theme` 状态机、localStorage 持久化（键 `askdata-theme`）、"跟随系统"三态、`matchMedia` 变更监听 |
| `web/index.html` | 已加**首屏防闪烁内联脚本**（第 9–30 行），在样式表前抢占写 `data-theme` |
| `web/src/main.ts` | 已 import 并调用 `initTheme()` |
| `web/src/views/WorkspaceView.vue` | 已加主题切换按钮，并已替换部分色值（53 → 32） |
| `web/src/components/AppIcon.vue` | 已加 `sun` / `moon` 图标 |
| `web/src/styles/global.css` | **已 100% token 化**（3 处已清零） |

`theme.ts` 第 9–14 行记录的约束值得保留：**必须是 `html` 上的 `data-theme`**，且内联脚本里的字符串 `'askdata-theme'` 与 `THEME_STORAGE_KEY` 必须一致。

### 发现 3：替换工作正在实时进行，行号会漂移

执行本报告期间，剩余硬编码数从 **158 → 136**（WorkspaceView 53→32，global.css 3→0）。

**因此：第 1 节的行号是 HEAD 基线行号，直接照抄行号去改当前文件会错位。请按"选择器 + 原色值"定位，不要只按行号定位。**

当前（报告完成时）各文件**尚未处理**的硬编码数：

| 文件 | 基线 | 剩余 | 状态 |
|---|---|---|---|
| `views/WorkspaceView.vue` | 53 | 32 | 进行中 |
| `components/TraceDrawer.vue` | 18 | 18 | 未开始 |
| `components/AnswerBubble.vue` | 18 | 18 | 未开始 |
| `components/EvidencePanel.vue` | 16 | 16 | 未开始 |
| `components/Composer.vue` | 14 | 14 | 未开始 |
| `components/PdfEvidenceViewer.vue` | 12 | 12 | 未开始 |
| `components/CitationList.vue` | 12 | 12 | 未开始 |
| `components/SqlEvidencePanel.vue` | 9 | 9 | 未开始 |
| `components/ClarifyPrompt.vue` | 2 | 2 | 未开始 |
| `components/ResultSetTable.vue` | 1 | 1 | 未开始 |
| `styles/global.css` | 3 | 0 | **已完成** |

---

## 1. 按文件分组的映射表

格式：`基线行号 | 原色值 | CSS 属性 | 所在选择器 | 语义角色 | 建议 token`

token 名列中：
- ✅ = **该 token 已存在于当前 `tokens.css`**，直接替换即可
- 🆕 = 需新增
- ⚠️ = 见第 3 节高风险清单，不能简单替换

### 1.1 `web/src/views/WorkspaceView.vue`（53 处）

| 行号 | 原色值 | 属性 | 选择器 | 语义角色 | 建议 token |
|---|---|---|---|---|---|
| 200 | `#fff` | background | `.sidebar` | 卡片表面 | `--bg-surface` ✅ |
| 202 | `#fff` | color | `.brand__mark` | 反白文字 | `--text-on-brand` ✅ |
| 202 | `#254ba6` | background | `.brand__mark` | 品牌深色 | `--brand-deep` ✅ |
| 202 | `#254ba61a` | box-shadow | `.brand__mark` | 阴影（品牌色低透明） | `--shadow-brand` ✅ ⚠️ |
| 203 | `#233b66` | color | `.brand b` | 主文字（品牌标题） | `--text-heading` ✅ |
| 205 | `#d8e3fc` | border | `.new-session` | 边框（品牌） | `--border-brand` ✅（注：亮色值不同，见 §2.4） |
| 205 | `#f2f5ff` | background | `.new-session` | 品牌浅底 | `--brand-soft` ✅ |
| 206 | `#e8efff` | background | `.new-session:hover` | 品牌浅底（悬停加深） | `--brand-soft-strong` ✅ |
| 207 | `#8c9cc1` | color | `.new-session small` | 弱化文字 | `--text-faint` ✅ |
| 208 | `#334a73` | color | `.workspace-label` | 次要文字 | `--text-secondary` ✅ |
| 217 | `#f5f7fb` | background | `.session-select:hover` | 悬停表面 | `--bg-hover` ✅ |
| 218 | `#315bc7` | color | `.session-select.active` | 品牌主色 | `--brand-primary` ✅ |
| 224 | `#e1e9fa` | background | `.session-more:hover` | 悬停表面（品牌调） | `--bg-subtle` ✅ |
| 226 | `#fff` | color | `.session-check.selected` | 反白文字 | `--text-on-brand` ✅ |
| 227 | `#1d2b4020` | box-shadow | `.session-menu` | 阴影（浮层） | `--shadow-menu` ✅ ⚠️ |
| 227 | `#fff` | background | `.session-menu` | 卡片表面（浮层） | `--bg-elevated` ✅ |
| 234 | `#fff` | background | `.batch-actions button` | 卡片表面 | `--bg-surface` ✅ |
| 236 | `#e7edf7` | border | `.source-note` | 边框（常规） | `--border-subtle` ✅ |
| 236 | `#f7f9fd` | background(gradient 起点) | `.source-note` | 次级表面 | ⚠️ 渐变，见 §3 |
| 236 | `#f2f5fc` | background(gradient 终点) | `.source-note` | 次级表面 | ⚠️ 渐变，见 §3 |
| 237 | `#405986` | color | `.source-note > div` | 次要文字 | `--text-secondary` ✅ |
| 241 | `#a8b2c2` | background | `.service-state i` | 中性状态点 | `--state-neutral` ✅ |
| 246 | `#ffffffd9` | background | `.topbar` | 半透明表面（玻璃态） | `--bg-glass` ✅ ⚠️ |
| 248 | `#3e4f69` | color | `.breadcrumb b` | 主文字 | `--text-primary` ✅ |
| 252 | `#fff` | background | `.evidence-button` | 卡片表面 | `--bg-surface` ✅ |
| 253 | `#cfdbf6` | border-color | `.evidence-button.active` | 边框（品牌） | `--border-brand` ✅ |
| 258 | `#d4dce8` | border-left | `.conversation__head p span` | 分隔线 | `--border-strong` ✅ |
| 259 | `#283b5a` | color | `.conversation__head h1` | 主文字（标题） | `--text-heading` ✅ |
| 263 | `#946524` | color | `.storage-warning` | 警告色 | `--state-warning` ✅ |
| 263 | `#fff3df` | background | `.storage-warning` | 警告浅底 | `--state-warning-soft` ✅ |
| 266 | `#6c7e98` | color | `.welcome__eyebrow` | 次要文字 | `--text-secondary` ✅ |
| 268 | `#223b63` | color | `.welcome h1` | 主文字（大标题） | `--text-heading` ✅ |
| 272 | `#e1e7f0` | border | `.example-card` | 边框（常规） | `--border-subtle` ✅ |
| 272 | `#ffffffea` | background | `.example-card` | 半透明表面 | `--bg-glass` ✅ ⚠️ |
| 272 | `#233b6603` | box-shadow | `.example-card` | 阴影（极淡） | `--shadow-card` ✅ ⚠️ |
| 273 | `#b9cbea` | border-color | `.example-card:hover` | 边框（强调） | `--border-brand` ✅ |
| 273 | `#233b6608` | box-shadow | `.example-card:hover` | 阴影（悬停） | 🆕 `--shadow-card-hover` ⚠️ |
| 275 | `#edf7f4` | background | `.example-card__icon` | 成功/青色浅底 | `--accent-teal-soft` ✅ |
| 276 | `#edf2ff` | background | `.example-card.blue .example-card__icon` | 品牌浅底 | `--brand-soft` ✅ |
| 279 | `#9aa9be` | color | `.example-card__arrow` | 弱化文字 | `--text-muted` ✅ |
| 281 | `#a0acbd` | color | `.welcome-path` | 弱化文字 | `--text-faint` ✅ |
| 282 | `#7b89a0` | color | `.welcome-path > span` | 次要文字 | `--text-secondary` ✅ |
| 283 | `#a9b5c6` | color | `.welcome-path small` | 弱化文字 | `--text-faint` ✅ |
| 284 | `#fff` | background | `.evidence` | 卡片表面 | `--bg-surface` ✅ |
| 286 | `#3e557d` | color | `.evidence__head > div` | 次要文字 | `--text-secondary` ✅ |
| 291 | `#17263e66` | background | `.guide-backdrop` | **遮罩层（scrim）** | `--scrim` ✅ ⚠️ |
| 292 | `#fff` | background | `.guide` | 卡片表面（弹窗） | `--bg-elevated` ✅ |
| 292 | `#16233a30` | box-shadow | `.guide` | 阴影（弹窗） | `--shadow-guide` ✅ ⚠️ |
| 295 | `#273e65` | color | `.guide h2` | 主文字（标题） | `--text-heading` ✅ |
| 302 | `#fff` | color | `.guide__confirm` | 反白文字 | `--text-on-brand` ✅ |
| 307 | `#233b6626` | box-shadow | `.evidence`(media) | 阴影（面板） | `--shadow-panel` ✅ ⚠️ |
| 308 | `#182b4640` | background | `.scrim`(media) | **遮罩层（scrim）** | `--scrim` ✅ ⚠️ |
| 314 | `#17263e26` | box-shadow | `.sidebar`(media) | 阴影（抽屉） | `--shadow-drawer` ✅ ⚠️ |

### 1.2 `web/src/components/TraceDrawer.vue`（18 处）

**整体特征**：该文件是"灰色梯度手搓"的重灾区——18 处里 **13 处是近似灰蓝**，其中 `#8b9ab0` 单独出现 3 次（37/44/45 行），另有 `#8a96a9`/`#8d9bb0`/`#94a2b7`/`#98a5b8`/`#7b8ca5` 等视觉上几乎无法区分的近邻。

| 行号 | 原色值 | 属性 | 选择器 | 语义角色 | 建议 token |
|---|---|---|---|---|---|
| 30 | `#edf0f5` | border-top | `.trace` | 分隔线 | `--border-subtle` ✅ |
| 31 | `#94a2b7` | color | `.trace-path` | 弱化文字 | `--text-muted` ✅ |
| 32 | `#7b8ca5` | color | `.trace-path > span:not(:first-child)` | 弱化文字 | `--text-muted` ✅ |
| 32 | `#f6f8fc` | background | 同上 | 次级表面（chip 底） | `--bg-subtle` ✅ |
| 33 | `#8a96a9` | color | `.trace-path__label` | 弱化文字 | `--text-muted` ✅ |
| 34 | `#7386a5` | color | `.trace__toggle` | 次要文字 | `--text-secondary` ✅ |
| 36 | `#8d9bb0` | color | `.trace__toggle small` | 弱化文字 | `--text-muted` ✅ |
| 37 | `#8b9ab0` | color | `.trace__time` | 弱化文字 | `--text-muted` ✅ |
| 41 | `#fbfcfe` | background | `.trace-step__head` | 次级表面 | `--bg-subtle` ✅ |
| 42 | `#dee6f3` | border | `.step-no` | 边框（常规） | `--border-subtle` ✅ |
| 42 | `#8496b4` | color | `.step-no` | 弱化文字 | `--text-muted` ✅ |
| 43 | `#647998` | color | `.trace-step__head b` | 次要文字 | `--text-secondary` ✅ |
| 44 | `#8b9ab0` | color | `.hit` | 弱化文字 | `--text-muted` ✅ |
| 45 | `#8b9ab0` | color | `time` | 弱化文字 | `--text-muted` ✅ |
| 46 | `#98a5b8` | color | `.trace-step__head > svg` | 弱化文字（图标） | `--text-muted` ✅ |
| 47 | `#fff` | background | `.trace-step__detail` | 卡片表面 | `--bg-surface` ✅ |
| 50 | `#7486a0` | color | `.trace-step__detail b` | 次要文字 | `--text-secondary` ✅ |
| 51 | `#f4f7fb` | background | `.trace-step__detail pre` | 次级表面（代码块） | `--bg-subtle` ✅ |

### 1.3 `web/src/components/AnswerBubble.vue`（18 处）

| 行号 | 原色值 | 属性 | 选择器 | 语义角色 | 建议 token |
|---|---|---|---|---|---|
| 65 | `#dfe7f8` | border | `.user` | 边框（用户气泡） | `--bubble-user-border` ✅ |
| 65 | `#2a466f` | color | `.user` | 主文字（用户气泡） | `--bubble-user-text` ✅ |
| 65 | `#eaf0fc` | background | `.user` | 用户气泡底 | `--bubble-user-bg` ✅ |
| 66 | `#fff` | background | `.assistant, .error` | 卡片表面 | `--bg-surface` ✅ |
| 67 | `#d2dcef` | border-color | `.assistant.selected` | 边框（强调/选中） | `--border-brand` ✅ |
| 68 | `#f0d9d4` | border-color | `.error` | 错误浅底/边框 | `--state-error-border` ✅ |
| 68 | `#fffcfb` | background | `.error` | 错误浅底 | `--state-error-soft` ✅（值不同，见 §2.5） |
| 71 | `#fceeea` | background | `.answer-mark--error` | 错误浅底 | `--state-error-soft` ✅ |
| 72 | `#2d4265` | color | `.answer-head > b` | 主文字 | `--text-heading` ✅ |
| 73 | `#7b8aa3` | color | `.answer-type` | 弱化文字 | `--text-muted` ✅ |
| 73 | `#f4f6fa` | background | `.answer-type` | 次级表面（标签底） | `--bg-subtle` ✅ |
| 74 | `#91a0b5` | color | `.copy-answer` | 弱化文字 | `--text-muted` ✅ |
| 77 | `#926255` | color | `.error .content` | 错误色（文字） | `--state-error-text` ✅ |
| 79 | `#dbe5fe` | background | `.inline-citation:hover/.active` | 品牌浅底（加深） | `--brand-soft-strong` ✅ |
| 83 | `#a0aabd` | color | `.thinking .waited` | 弱化文字 | `--text-faint` ✅ |
| 89 | `#ead1ca` | border | `.retry` | 错误边框 | `--state-error-border` ✅ |
| 89 | `#a36a58` | color | `.retry` | 错误文字 | `--state-error-text` ✅ |
| 89 | `#fff` | background | `.retry` | 卡片表面 | `--bg-surface` ✅ |

### 1.4 `web/src/components/EvidencePanel.vue`（16 处）

| 行号 | 原色值 | 属性 | 选择器 | 语义角色 | 建议 token |
|---|---|---|---|---|---|
| 23 | `#fff` | background | `.evidence-panel` | 卡片表面 | `--bg-surface` ✅ |
| 25 | `#e0e8f7` | border | `.evidence-empty > span` | 边框（常规） | `--border-subtle` ✅ |
| 25 | `#8fa5cf` | color | `.evidence-empty > span` | 弱化文字（空态图标） | `--text-muted` ✅ |
| 25 | `#f5f8fd` | background | `.evidence-empty > span` | 次级表面 | `--bg-subtle` ✅ |
| 26 | `#526987` | color | `.evidence-empty h2` | 主文字 | `--text-heading` ✅ |
| 27 | `#929eb0` | color | `.evidence-empty p` | 弱化文字 | `--text-muted` ✅ |
| 28 | `#f9fbff` | background | `.quoted-snippet` | 次级表面 | `--bg-subtle` ✅ |
| 29 | `#6e82a3` | color | `.quoted-snippet summary` | 次要文字 | `--text-secondary` ✅ |
| 30 | `#9aa8bc` | color | `.quoted-snippet small` | 弱化文字 | `--text-muted` ✅ |
| 31 | `#ccdaef` | border-left | `.quoted-snippet blockquote` | 边框（引用条） | `--border-brand` ✅ |
| 34 | `#2f466b` | color | `.doc-fallback h3` | 主文字（标题） | `--text-heading` ✅ |
| 36 | `#8493aa` | color | `.fragment-label` | 弱化文字 | `--text-muted` ✅ |
| 37 | `#e5dfd3` | border | `.doc-fallback blockquote` | 警告边框 | `--state-warning-border` ✅ |
| 37 | `#726653` | color | 同上 | 警告色（文字） | `--state-warning` ✅（值不同，见 §2.5） |
| 37 | `#fffbf2` | background | 同上 | 警告浅底 | `--state-warning-soft` ✅（值不同，见 §2.5） |
| 38 | `#a0abba` | color | `.fragment-note` | 弱化文字 | `--text-faint` ✅ |

### 1.5 `web/src/components/Composer.vue`（14 处）

| 行号 | 原色值 | 属性 | 选择器 | 语义角色 | 建议 token |
|---|---|---|---|---|---|
| 32 | `#dce4ee` | border | `.composer` | 边框（输入框） | `--border-subtle` ✅ |
| 32 | `#fff` | background | `.composer` | 输入框背景 | `--bg-surface` ✅ |
| 32 | `#233b6606` | box-shadow | `.composer` | 阴影（低透明叠加） | `--shadow-card` ✅ ⚠️ |
| 33 | `#8da9e4` | border-color | `.composer:focus-within` | **边框（聚焦）** | 🆕 `--border-focus` ⚠️ |
| 33 | `#315bc709` | box-shadow | `.composer:focus-within` | **聚焦环**（品牌色极低透明） | 🆕 `--shadow-focus-ring` ⚠️ |
| 33 | `#233b6606` | box-shadow | `.composer:focus-within` | 阴影（同 32 行） | `--shadow-card` ✅ ⚠️ |
| 35 | `#8a9bb8` | color | `.composer__spark` | 弱化文字（图标） | `--text-muted` ✅ |
| 37 | `#95a1b5` | color | `textarea::placeholder` | **弱化文字（占位符）** | `--text-muted` ✅ |
| 39 | `#9ca7b8` | color | `.composer__bottom > span` | 弱化文字 | `--text-faint` ✅ |
| 40 | `#fff` | color | `button` | 反白文字 | `--text-on-brand` ✅ |
| 41 | `#274eb4` | background | `button:hover` | 品牌深色（悬停） | 🆕 `--brand-primary-hover` ⚠️ |
| 42 | `#b0bdd3` | color | `button:disabled` | 弱化文字（禁用） | `--text-faint` ✅ |
| 42 | `#eff3f9` | background | `button:disabled` | 次级表面（禁用） | `--bg-subtle` ✅ |
| 44 | `#e6ecf5` | background | `button.stop-button:hover` | 悬停表面 | `--bg-hover` ✅ |

### 1.6 `web/src/components/PdfEvidenceViewer.vue`（12 处）

**PDF 专项分栏**（见 §3 高风险）：

| 行号 | 原色值 | 属性 | 选择器 | 语义角色 | 归属 | 建议 token |
|---|---|---|---|---|---|---|
| 176 | `#7c8eaa` | color | `header p` | 弱化文字 | 界面 | `--text-muted` ✅ |
| 177 | `#3c537a` | color | `header strong` | 主文字 | 界面 | `--text-heading` ✅ |
| 179 | `#fff` | background | `.zoom button, .page-controls button` | 卡片表面（控件） | 界面（页码栏/缩放） | `--bg-surface` ✅ |
| 181 | `#ccdaf5` | border-color | `.zoom .selected` | 边框（品牌/选中） | 界面（缩放控件） | `--border-brand` ✅ |
| 182 | `#8a9ab1` | color | `.page-controls` | 弱化文字（页码栏） | 界面（页码栏） | `--text-muted` ✅ |
| 185 | `#9b8356` | color | `.notice` | 警告色 | 界面 | `--state-warning` ✅ |
| 185 | `#fcf7eb` | background | `.notice` | 警告浅底 | 界面 | `--state-warning-soft` ✅（值不同） |
| **186** | **`#edf0f5`** | **background** | **`.pdf-scroll`** | **PDF 页面外的容器** | **界面——应跟随主题** | **`--bg-paper-stage` ✅** |
| 187 | `#233b6610` | box-shadow | `.pdf-page` | 纸张投影（低透明叠加） | 界面 | `--shadow-paper` ✅ ⚠️ |
| **187** | **`#fff`** | **background** | **`.pdf-page`** | **PDF 纸张本体** | **内容——不该跟随主题** | **`--surface-paper` ✅（两套主题都保持 `#ffffff`）** |
| 189 | `#efc2bb` | border | `.error` | 错误边框 | 界面 | `--state-error-border` ✅ |
| 189 | `#fff6f4` | background | `.error` | 错误浅底 | 界面 | `--state-error-soft` ✅ |

> **PDF 关键区分**（任务要求特别指出）：
> - **`pdf-scroll`（186 行，`#edf0f5`）= PDF 页面外的舞台容器**，属于界面，暗色下必须变暗 → 用 `--bg-paper-stage`（暗色值 `#0f1319`）。
> - **`pdf-page`（187 行，`#fff`）= PDF 纸张本体**，是**被渲染的文档内容**，不是界面 → 用 `--surface-paper`，**两套主题下都保持白色**。
> - `tokens.css` 文件头第 12–13 行已明文记录这条设计决策："`--surface-paper`（PDF 纸张）在两套主题下都是白色：它是被渲染的文档内容本身，不是界面的一部分，跟着主题变会让白底黑字的 PDF 看起来像浮在黑框里。"
> - **`PdfHighlightLayer.vue` 属于"PDF 内容覆盖层"**：它叠在白色画布上，用的是已 token 化的 `--evidence-amber` / `--evidence-amber-soft`，暗色下 token 值改成低透明度亮黄（第 156–160 行有注释说明），**不要**让它跟随界面主题变暗。

### 1.7 `web/src/components/CitationList.vue`（12 处）

| 行号 | 原色值 | 属性 | 选择器 | 语义角色 | 建议 token |
|---|---|---|---|---|---|
| 29 | `#74829a` | color | `.citations header` | 次要文字 | `--text-secondary` ✅ |
| 32 | `#8b98ab` | color | `.citation-tip` | 弱化文字 | `--text-muted` ✅ |
| 35 | `#e5eaf2` | border | `.citation` | 边框（常规） | `--border-subtle` ✅ |
| 35 | `#fafbfd` | background | `.citation` | 次级表面 | `--bg-subtle` ✅ |
| 36 | `#c4d4f4` | border-color | `.citation:hover/.active` | 边框（强调） | `--border-brand` ✅ |
| 36 | `#f2f6ff` | background | `.citation:hover/.active` | 悬停表面（品牌调） | `--brand-soft` ✅ |
| 37 | `#e9effb` | background | `.citation__number` | 品牌浅底 | `--brand-soft` ✅ |
| 39 | `#526581` | color | `.citation__body b` | 主文字 | `--text-heading` ✅ |
| 40 | `#8493a9` | color | `.citation__meta` | 弱化文字 | `--text-muted` ✅ |
| 42 | `#7d8a9e` | color | `.citation__body p` | 次要文字 | `--text-secondary` ✅ |
| 43 | `#9aa9bd` | color | `.citation__arrow` | 弱化文字 | `--text-muted` ✅ |
| 44 | `#7188b4` | color | `.citation-more` | 链接（品牌弱） | `--brand-primary` ✅ |

### 1.8 `web/src/components/SqlEvidencePanel.vue`（9 处）

| 行号 | 原色值 | 属性 | 选择器 | 语义角色 | 建议 token |
|---|---|---|---|---|---|
| 20 | `#2f466b` | color | `h3` | 主文字（标题） | `--text-heading` ✅ |
| 24 | `#607495` | color | `dd` | 次要文字 | `--text-secondary` ✅ |
| 25 | `#8b9ab0` | color | `.section-label` | 弱化文字 | `--text-muted` ✅ |
| 26 | `#a8b1bf` | color | `.section-label small` | 弱化文字 | `--text-faint` ✅ |
| 27 | `#dbe5f5` | border | `blockquote` | 边框（品牌） | `--border-brand` ✅ |
| 27 | `#58749f` | color | `blockquote` | 主文字 | `--text-primary` ✅ |
| 27 | `#f5f8fe` | background | `blockquote` | 品牌浅底 | `--brand-soft` ✅ |
| **29** | **`#d3e0fa`** | **color** | **`pre`** | **反白文字（代码）** | **`--text-on-brand` ✅** |
| **29** | **`#213451`** | **background** | **`pre`** | **代码块深底** | **🆕 `--code-bg` ⚠️** |

> 29 行是**唯一一处"亮色界面里的深色块"**——SQL 代码块刻意用深底浅字。暗色下界面本身变暗，若只把 `--text-on-brand` 保持白色、背景不换，会变成"深底白字浮在深色界面里"仍能看，但层次消失。建议**单独建 `--code-bg` / `--code-text` 一对 token**，暗色下让代码块比 `--bg-surface` 再深一档（或相反，更亮一档）以保留对比。**不要**把它映射到 `--bg-app`。

### 1.9 `web/src/components/ClarifyPrompt.vue`（2 处）

| 行号 | 原色值 | 属性 | 选择器 | 语义角色 | 建议 token |
|---|---|---|---|---|---|
| 9 | `#b8c8df` | border | `.clarify` | 边框（强调/提示） | `--border-brand` ✅ |
| 9 | `#f5f8fd` | background | `.clarify` | 次级表面 | `--bg-subtle` ✅ |

### 1.10 `web/src/components/ResultSetTable.vue`（1 处）

| 行号 | 原色值 | 属性 | 选择器 | 语义角色 | 建议 token |
|---|---|---|---|---|---|
| 93 | `#f6f8fb` | background（**fallback 值**） | `.result-head, .pager` | 次级表面 | 直接改用 `var(--bg-subtle)` 并**删掉 fallback** ✅ |

> 原文是 `background: var(--bg-subtle, #f6f8fb);`——已有变量，只是带了个硬编码兜底。删掉 `, #f6f8fb` 即可。

### 1.11 `web/src/styles/global.css`（3 处，任务清单遗漏）

**状态：已被并行 agent 处理完毕（当前 0 处）。** 记录基线值备查：

| 行号 | 原色值 | 属性 | 选择器 | 语义角色 | 建议 token |
|---|---|---|---|---|---|
| 11 | `#183578` | color | `::selection` | 反白/选中文字 | `--text-on-brand` ✅ |
| 11 | `#dce6ff` | background | `::selection` | 品牌浅底（选中） | `--brand-soft-strong` ✅ |
| 12 | `#c8d1df` | scrollbar-color | `*` | 边框（滚动条） | `--border-strong` ✅ |

> 当前实现已按此替换（`global.css:14-15`）。另注：`::selection` 的语义在暗色下要留神——`--brand-soft-strong` 在暗色是 `#263553`，配 `--text-on-brand`（白）可读性没问题。

---

## 2. 建议的语义 token 清单（对照现有 `tokens.css`）

### 2.1 背景类

| token | 覆盖的原色值 | 状态 |
|---|---|---|
| `--bg-app` | 页面底（tokens.css 已定义 `#f5f7fa`） | ✅ 已有 |
| `--bg-surface` | `#fff`（卡片/面板/按钮面） | ✅ 已有 |
| `--bg-subtle` | `#f6f8fc` `#fbfcfe` `#f4f7fb` `#f4f6fa` `#fafbfd` `#f5f8fd` `#f9fbff` `#f6f8fb` `#eff3f9` | ✅ 已有 |
| `--bg-hover` | `#f5f7fb` `#e6ecf5` | ✅ 已有 |
| `--bg-elevated` | `#fff`（浮层：菜单/弹窗） | ✅ 已有 |
| `--bg-glass` | `#ffffffd9` `#ffffffea` | ✅ 已有 |
| `--bg-paper-stage` | `#edf0f5`（PDF 舞台） | ✅ 已有 |
| `--surface-paper` | `#fff`（PDF 纸张，两主题恒白） | ✅ 已有 |

### 2.2 文字类

| token | 覆盖的原色值 | 状态 |
|---|---|---|
| `--text-primary` | `#58749f` `#3e4f69` | ✅ 已有 |
| `--text-heading` | `#233b66` `#283b5a` `#223b63` `#273e65` `#2d4265` `#2f466b` `#526581` `#526987` `#3c537a` | ✅ 已有 |
| `--text-secondary` | `#637086` `#334a73` `#405986` `#6c7e98` `#7b89a0` `#3e557d` `#6e82a3` `#74829a` `#7d8a9e` `#7386a5` `#647998` `#7486a0` `#607495` | ✅ 已有 |
| `--text-muted` | `#8c9cc1` `#9aa9be` `#8b98ab` `#8493a9` `#9aa9bd` `#8493aa` `#91a0b5` `#7b8aa3` `#8fa5cf` `#929eb0` `#9aa8bc` `#8b9ab0`×3 `#8a96a9` `#8d9bb0` `#94a2b7` `#7b8ca5` `#98a5b8` `#8496b4` `#8a9bb8` `#95a1b5` `#8a9ab1` `#7c8eaa` | ✅ 已有 |
| `--text-faint` | `#a0acbd` `#a9b5c6` `#a0aabd` `#a0abba` `#a8b1bf` `#9ca7b8` `#b0bdd3` | ✅ 已有 |
| `--text-on-brand` | `#fff`（出现在 color 上：反白） | ✅ 已有 |

> **`--text-muted` 是最大受益者**：26 个肉眼难辨的灰蓝全部收敛为 1 个 token。这正是任务描述里说的"手搓同一套灰色梯度"。

### 2.3 边框类

| token | 覆盖的原色值 | 状态 |
|---|---|---|
| `--border-subtle` | `#e4e9f0` `#e1e7f0` `#e5eaf2` `#e7edf7` `#dce4ee` `#dee6f3` `#e0e8f7` `#edf0f5` | ✅ 已有 |
| `--border-strong` | `#d4dce8` `#c8d1df` | ✅ 已有 |
| `--border-brand` | `#cfdbf6` `#d8e3fc` `#c4d4f4` `#b9cbea` `#ccdaf5` `#ccdaef` `#dbe5f5` `#b8c8df` `#d2dcef` | ✅ 已有 |
| `--border-focus` | `#8da9e4`（`:focus-within` 输入框聚焦边） | 🆕 **需新增** |
| 分隔线 | `#d4dce8` `#edf0f5`（`border-top`/`border-left` 场景） | ✅ 复用 `--border-strong` / `--border-subtle` |

### 2.4 品牌类

| token | 覆盖的原色值 | 状态 |
|---|---|---|
| `--brand-primary` | `#315bc7` `#7188b4` | ✅ 已有 |
| `--brand-primary-weak` | `#5279dc` | ✅ 已有 |
| `--brand-soft` | `#edf2ff` `#f2f5ff` `#e9effb` `#f2f6ff` `#f5f8fe` | ✅ 已有 |
| `--brand-soft-strong` | `#e8efff` `#e1e9fa` `#dbe5fe` `#dce6ff` | ✅ 已有 |
| `--brand-deep` | `#254ba6`（品牌深底，如 logo 块） | ✅ 已有 |
| `--brand-primary-hover` | `#274eb4`（主按钮悬停） | 🆕 **需新增** |
| `--accent-teal` / `--accent-teal-soft` | `#168b82` / `#edf7f4` | ✅ 已有 |

### 2.5 状态类

| token | 覆盖的原色值 | 状态 |
|---|---|---|
| `--state-error` | `#c8452f` | ✅ 已有 |
| `--state-error-soft` | `#fceeea` `#fffcfb` `#fff6f4` | ✅ 已有（值 `#fceeea`，`#fffcfb`/`#fff6f4` 是近邻，统一即可） |
| `--state-error-border` | `#f0d9d4` `#ead1ca` `#efc2bb` | ✅ 已有（值 `#f0d9d4`，统一） |
| `--state-error-text` | `#926255` `#a36a58` | ✅ 已有（值 `#926255`） |
| `--state-success` | `#2e8b57` | ✅ 已有 |
| `--state-success-soft` | `#eaf6f0` | ✅ 已有 |
| `--state-warning` | `#946524` `#726653` `#9b8356` | ✅ 已有（值 `#946524`） |
| `--state-warning-soft` | `#fff3df` `#fffbf2` `#fcf7eb` | ✅ 已有（值 `#fff3df`） |
| `--state-warning-border` | `#e5dfd3` | ✅ 已有 |
| `--state-neutral` | `#a8b2c2` | ✅ 已有 |

> ⚠️ 状态浅底一族存在**轻微色相分叉**：`#fff3df` 偏橙（storage-warning）、`#fffbf2` 偏暖白（doc-fallback 引用）、`#fcf7eb` 偏黄（PDF notice）。统一到 `--state-warning-soft` 会**损失一点语义区分**（"存储告警"vs"普通引用"）。若在意，可拆 `--state-warning-soft` 与 `--quote-paper-bg` 两个。**我倾向统一，因为三者在视觉上本就几乎不可分。**

### 2.6 特殊类

| token | 覆盖的原色值 | 状态 |
|---|---|---|
| `--evidence-amber` / `--evidence-amber-soft` | 已被 `PdfHighlightLayer.vue` 使用 | ✅ 已有 |
| `--evidence-highlight` | 暗色下低透明度亮黄 | ✅ 已有 |
| `--bubble-user-bg` / `--bubble-user-border` / `--bubble-user-text` | `#eaf0fc` / `#dfe7f8` / `#2a466f` | ✅ 已有 |
| `--scrim` | `#17263e66` `#182b4640` | ✅ 已有 |
| `--shadow-card` | `#233b6603` `#233b6606`×2 | ✅ 已有 |
| `--shadow-menu` | `#1d2b4020` | ✅ 已有 |
| `--shadow-panel` | `#233b6626` | ✅ 已有 |
| `--shadow-guide` | `#16233a30` | ✅ 已有 |
| `--shadow-brand` | `#254ba61a` | ✅ 已有 |
| `--shadow-drawer` | `#17263e26` | ✅ 已有 |
| `--shadow-paper` | `#233b6610` | ✅ 已有 |
| `--shadow-card-hover` | `#233b6608` | 🆕 **需新增**（悬停时阴影略重） |
| `--shadow-focus-ring` | `#315bc709` | 🆕 **需新增**（聚焦环） |
| `--code-bg` / `--code-text` | `#213451` / `#d3e0fa` | 🆕 **需新增**（SQL 代码块） |

### 2.7 需新增 token 汇总（共 6 个）

```
--border-focus          #8da9e4
--brand-primary-hover   #274eb4
--shadow-card-hover     0 8px 24px <原 #233b6608>
--shadow-focus-ring     0 0 0 3px <原 #315bc709>
--code-bg               #213451
--code-text             #d3e0fa
```

其余 **~152 处全部可由现有 token 覆盖**，无需新增。

---

## 3. 高风险处清单（不能简单换 token）

### 3.1 带 alpha 的 8 位 hex 叠加（14 处）——最高风险

这些是"某色的低透明度叠加"，**暗色下逻辑相反**（任务描述已正确指出：低透明白=高亮，低透明黑=阴影）。全清单：

| 位置 | 原值 | 解读 | 风险与处理 |
|---|---|---|---|
| `WorkspaceView.vue:202` | `#254ba61a` | brand-deep 10% | → `--shadow-brand` ✅ 已有专用 token，**不要**试图用 opacity 表达 |
| `WorkspaceView.vue:227` | `#1d2b4020` | text-primary 12.5%，浮层影 | → `--shadow-menu` ✅ |
| `WorkspaceView.vue:246` | `#ffffffd9` | 白 85%，玻璃顶栏 | → `--bg-glass` ✅ **暗色下必须是 `rgba(28,34,44,.85)` 而非白** |
| `WorkspaceView.vue:272` | `#ffffffea` | 白 92%，卡片面 | → `--bg-glass` ✅ 同上 |
| `WorkspaceView.vue:272` | `#233b6603` | 深蓝 1.2%，极淡影 | → `--shadow-card` ✅ |
| `WorkspaceView.vue:273` | `#233b6608` | 深蓝 3%，悬停影 | → 🆕 `--shadow-card-hover` |
| `WorkspaceView.vue:291` | `#17263e66` | 深蓝 40%，**遮罩** | → `--scrim` ✅ **暗色下应是纯黑 60%** |
| `WorkspaceView.vue:292` | `#16233a30` | 深蓝 19%，弹窗影 | → `--shadow-guide` ✅ |
| `WorkspaceView.vue:307` | `#233b6626` | 深蓝 15%，面板影 | → `--shadow-panel` ✅ |
| `WorkspaceView.vue:308` | `#182b4640` | 深蓝 25%，**遮罩** | → `--scrim` ✅ |
| `WorkspaceView.vue:314` | `#17263e26` | 深蓝 15%，抽屉影 | → `--shadow-drawer` ✅ |
| `Composer.vue:32` | `#233b6606` | 深蓝 2.4%，输入框影 | → `--shadow-card` ✅ |
| `Composer.vue:33` | `#233b6606` | 同上（第二重影） | → `--shadow-card` ✅ |
| `Composer.vue:33` | `#315bc709` | 品牌色 3.5%，**聚焦环** | → 🆕 `--shadow-focus-ring` **暗色下需改亮色低透明** |
| `PdfEvidenceViewer.vue:187` | `#233b6610` | 深蓝 6%，纸张影 | → `--shadow-paper` ✅ |

**核心风险**：`#315bc709`（品牌色聚焦环）在暗色下若原样保留，几乎不可见（暗底 + 3.5% 深蓝 ≈ 无）。必须像 `--state-*` 那样给出暗色专用值（例如品牌亮色 `rgba(109,148,232,.35)`）。当前 `tokens.css` 的暗色块里 **`--shadow-card`/`--shadow-menu`/`--shadow-panel`/`--shadow-guide`/`--shadow-brand`/`--shadow-drawer`/`--shadow-paper` 都已给出暗色值（纯黑叠加）**，说明这条已处理到位；但 `--shadow-card-hover` 与 `--shadow-focus-ring` 是**新增的，必须同步补暗色值**，否则漏一处。

### 3.2 渐变（1 处）

`WorkspaceView.vue:236`：
```css
.source-note { ... background: linear-gradient(120deg, #f7f9fd, #f2f5fc); }
```
- **风险**：这是**两色渐变**，不是单色。若粗暴替换成 `background: var(--bg-subtle)`，会**丢掉渐变效果**；若替换成 `linear-gradient(120deg, var(--bg-subtle), var(--bg-subtle))` 则等同纯色，同样丢失。
- **建议**：暗色下渐变意义不大（暗色下细微渐变几乎不可见），**推荐降级为纯色 `--bg-subtle`**——这属于有意的视觉简化，应在提交说明里写明。若要保留，则需新增 `--surface-gradient-from` / `--surface-gradient-to` 两个 token。
- 另注：该行 `border: 1px solid #e7edf7` 与渐变**同一行**，替换时容易只改颜色漏掉渐变。

### 3.3 PDF 覆盖层（`PdfHighlightLayer.vue` + `PdfEvidenceViewer.vue`）

已在 §1.6 说明。补充风险点：
- `PdfHighlightLayer.vue:17` 用 `--evidence-amber` / `--evidence-amber-soft`，**它叠在白色 PDF 画布上**。暗色下若让这两个 token 跟随界面变暗，会盖不住白底或变成脏色。
- 当前 `tokens.css` 暗色块（158–160 行）已正确处理：`--evidence-amber-soft: rgba(232,169,74,.22)` **保持半透明**，并注释"暗色下必须保持半透明：它是叠在 PDF 白色画布上的，用不透明实色会盖掉文字"。
- **不要**把 `.pdf-page` 的 `#fff` 换成 `--bg-surface`。</br>**不要**把 `.pdf-scroll` 的 `#edf0f5` 换成 `--bg-app`（它是为白纸专门配的舞台色，暗色下应是 `#0f1319` 而不是 `--bg-app` 的 `#14181f`，虽然接近但语义不同）。

### 3.4 `box-shadow` 里的多重颜色（2 处）

| 位置 | 原值 | 风险 |
|---|---|---|
| `Composer.vue:33` | `box-shadow: 0 0 0 3px #315bc709, 0 5px 25px #233b6606;` | **一条声明里两个颜色**，分属"聚焦环"和"卡片影"两种语义。必须拆成两个 token：`var(--shadow-focus-ring), var(--shadow-card)`。若整体替换成一个 token 会丢失其中一个语义。 |
| `WorkspaceView.vue:272/273` | `box-shadow: 0 3px 15px #233b6603;` / `0 8px 24px #233b6608;` | 单色但**模糊半径/偏移不同**。若都映射到 `--shadow-card`，悬停态会**看不出变化**（因为 `--shadow-card` 是 `0 3px 14px`）。所以 `273` 行必须用新增的 `--shadow-card-hover`。 |

### 3.5 SVG 内联颜色（已核查：无）

`AppIcon.vue` 使用 `stroke="currentColor"`，**没有任何内联色值**——所有图标颜色都由外层 CSS 的 `color` 决定。核查结论：**SVG 内联颜色不是本项目风险源**。

同理，`PdfHighlightLayer.vue` 的 `<i>` 元素样式全部走 CSS 变量，无内联色。

### 3.6 `index.html` 的两处颜色（非 CSS 变量体系）

| 位置 | 值 | 说明 |
|---|---|---|
| `index.html:6` | `<meta name="theme-color" content="#315bc7">` | 移动端地址栏颜色。**不能**用 CSS 变量（meta 标签不吃 var）。已在 `index.html:22-23` 的内联脚本 + `theme.ts:49` 两处以 JS 硬编码切到 `#14181f`。**这两处 JS 硬编码必须与 `--bg-app` 的暗色值保持一致**，改 `--bg-app` 时要同步。 |
| `index.html:14-30` | 内联防闪烁脚本 | 存储键字符串 `'askdata-theme'` 与 `theme.ts` 的 `THEME_STORAGE_KEY` 必须一致（两处注释都强调了）。 |

### 3.7 其他需要注意的

- **`ResultSetTable.vue:98`**：`background: white;`（`.pager button`）——用的是**颜色关键字 `white` 而非 `#fff`**，我的正则和常规 grep 都可能漏掉。**必须手工处理成 `var(--bg-surface)`**。这是全项目唯一一处关键字形式。
- **`ResultSetTable.vue:93`**：`var(--bg-subtle, #f6f8fb)` 的 fallback，删掉即可。
- **`WorkspaceView.vue:228`**：`.session-check { color: transparent; }`——`transparent` 不用换。
- **`.trace-step__detail pre`（TraceDrawer:51）/ `.pdf-scroll`（PdfEvidenceViewer:186）** 都是滚动容器底色，暗色下若映射错会成为"亮块"，建议改完在暗色下目视核对。
- **`SqlEvidencePanel.vue:29` 代码块**：见 §1.8，是唯一的"亮色界面里的深色块"，暗色下层次关系会反转，需专门设计。

---

## 4. 执行建议

1. **不要按行号批量替换**——文件正在被并行修改，行号已漂移。请按"选择器 + 原色值"定位。
2. **`#8b9ab0` 在 `TraceDrawer.vue` 出现 3 次（37/44/45 行）**，可用 replace_all；但 `#fff` 跨文件出现 17 次且**语义各异**（表面 12 次 / 反白文字 4 次 / 纸张 1 次），**绝对不能用 replace_all**。
3. 先补 6 个新 token（含**暗色值**），再逐文件替换，避免中途出现未定义变量。
4. 替换后**必须在暗色下逐文件目视核对**，重点看：PDF 面板（§3.3）、Composer 聚焦态（§3.4）、SQL 代码块（§1.8）、各滚动容器。
5. 验证手段：`grep -nE '#[0-9a-fA-F]{3,8}|rgba?\(|:\s*white\b' web/src --include=*.vue --include=*.css | grep -v tokens.css` 应只剩 `tokens.css` 内的定义。
