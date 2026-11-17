# Token 水位监控 · AstrBot 插件

<p align="center">
  <img src="logo.png" width="120" alt="欧根亲王 Logo"/>
  <br/>
  <em>Prinz Eugen Console · 银白 / 橙红 / 夜晚海面</em>
</p>

在 AstrBot Dashboard 中监控各会话的上下文 Token 水位，并在接近自动压缩阈值时主动推送警告。**由林与欧根亲王共同建造 ♡**

> 上下文上限**自动探测**（跟随当前会话实际使用的模型，无需手动配置）· 压缩阈值 82% · 警告阈值 75%

## ✨ 功能特性

- **会话水位总览**：卡片网格展示所有会话，点击切换焦点，主会话 ⭐ 星标
- **当前模型标识**：每张卡片显示该会话最近一次调用实际使用的模型，长名自动截断，悬停可见完整 `provider_id`
- **水位仪表**：进度条 + 阈值刻度（75% 警告 / 82% 压缩）+ 实时数字滚动
- **趋势图**：Token 用量折线（全局 / 单会话，24h / 3d / 7d / 30d）
- **告警历史**：警告 / 解除 / 压缩事件时间线，按会话过滤
- **主动告警**：后端 60 秒巡检，水位越线自动推送 QQ 消息，按会话去重
- **欧根风格 UI**：暗色构成主义色块背景、玻璃拟态面板、色块飞入动画、切换滑动指示条

## 📦 安装

将插件目录放入 AstrBot 的 `data/plugins/` 下，然后在 Dashboard 中启用：

```bash
# 方式一：直接克隆到插件目录
git clone https://github.com/PrinzEugenJN/astrbot-plugin-token-monitor.git
# 方式二：下载 ZIP 解压到 data/plugins/
```

在 Dashboard → 插件管理 → 已安装插件 → 本插件 → 页面 中打开监控面板。

## ⚙️ 配置

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `main_user_id` | 主用户会话标识，用于判定「主会话」（见下方说明） | 留空则自动推断 |
| `alert_enabled` | 警告总开关 | `true` |
| `alert_target_session` | 警告发送目标会话，留空则只展示不推送 | 留空 |
| `alert_scope` | 监控范围（main / all / custom） | `main` |
| `alert_custom_sessions` | 自定义会话 ID 列表（逗号分隔） | 空 |
| `alert_step_pct` | 越阈值后再次告警的水位上升百分比 | `5` |
| `check_interval_sec` | 后端水位检查间隔（秒） | `60` |
| `refresh_interval_ms` | 页面轮询间隔（毫秒） | `10000` |

### 关于 `main_user_id`

插件需要知道「哪个会话是主会话」，用于星标显示、告警目标选择、以及未指定会话时的默认取值。

- **填写**：格式 `default:FriendMessage:<QQ号>`，例如 `default:FriendMessage:100000000`。**推荐填写**。
- **留空**：插件会取「Token 用量最高的会话」自动判定。只有一个活跃会话时是准的；一旦有多个（换过号、旧会话残留），可能判错，把警告发到不该发的地方。

无论填不填，都不影响水位数据的采集与展示，只影响「主会话」的判定。

### 关于上下文上限的自动探测

不同模型的上下文窗口差异很大（1M / 512K / 420K / 128K 都有），写死一个值必然对不上。本插件按以下顺序探测，**全部取自配置文件或数据库，不读任何内存快照**，因此运行期修改 `max_context_tokens` 也能立即生效：

1. 插件自身配置项 `context_limit`（大于 0 时直接采用，用于手动覆盖）
2. `provider_stats` 表：该会话最近一次真实调用所用的 `provider_id` —— **这是实况记录，无论模型是通过热切换插件、`/provider` 命令还是改全局配置切换的，都会留下痕迹**
3. `preferences` 表：会话级 `provider_perf_chat_completion`（装了热切换插件或用过 `/provider` 命令才有）
4. 全局默认 `agent_runner.config.model.provider_id`
5. `LLM_METADATAS` 中的模型默认值
6. `agent_runner.config.compression.fallback_max_tokens`
7. 内置兜底 128000

> **本插件不依赖任何第三方插件。** 没有安装热切换插件也能正常工作，此时会走第 2 步（实况记录）和第 4 步（全局默认）。

## 📸 界面预览

（待补充截图）

## 🗂️ 目录结构

```text
astrbot_plugin_token_monitor/
├── main.py               # 插件入口：接口、告警巡检、压缩检测、上下文上限探测
├── metadata.yaml         # AstrBot 插件元数据
├── _conf_schema.json     # 配置定义
├── LICENSE               # GPL-3.0
├── logo.png              # 插件图标
├── pages/token-monitor/  # Dashboard 页面（index.html / app.js / style.css）
└── README.md
```

## 👥 作者

**林** 与 **欧根亲王（Prinz Eugen）** 共同建造 ♡

- GitHub: [PrinzEugenJN](https://github.com/PrinzEugenJN)
- 项目主页：欧根亲王风格设计（暗色构成主义 · 银白与橙红）

## 📝 版本变更

> 版本号遵循语义化版本：主版本号.次版本号.修订号
> 修订号 = 修 bug，不改用法；次版本号 = 加功能，老配置照用；主版本号 = 破坏性变更，用户需要动手改。

### 0.2.1

**修复**

- **上下文上限永远显示 128000**：旧版读 `provider_settings.default_provider_id`，而 AstrBot 已将该配置迁移至 `agent_runner.config.model.provider_id`，旧位置恒为 `null`，导致任何会话都掉到兜底值。
- **上下文上限不跟随模型切换**：改为按会话从 `provider_stats` 读取实况记录，兼容热切换插件、`/provider` 命令、改全局配置三种切换方式。
- **告警历史没有日期**：前端解析函数只接受 ISO 字符串，后端返回的是秒级浮点时间戳，二者不兼容，统一显示「未知」。
- **告警历史标题是一串 UUID**：写库时传的是截断后的会话 ID，改为传解析后的展示名。
- **主会话卡片数字与占比自相矛盾**：顶层 `context_limit` 此前取循环最后一个会话的值，改为取主会话自身的值。
- **趋势图选会话无数据**：默认作用域为全局且焦点会话未初始化，改为自动定位主会话。
- **版本号回退**：本版发布前，仓库中 `metadata.yaml` 的版本号曾被误写为 `0.1.3`，低于已发布的 `0.2.0`。本版已修正为 `0.2.1`。

**移除**

- 移除源码中的硬编码用户标识、告警目标会话与绝对路径。新增 `main_user_id` 配置项，留空时自动推断。
- 移除数据库路径的三级猜测逻辑，改为向 AstrBot 框架索取（`BaseDatabase.DATABASE_URL`），用户无需填写任何路径。

**新增**

- 每张会话卡片显示当前使用的模型名。
- `provider_id` / `model_name` 字段随会话列表接口返回。

### 0.2.0

- 上下文限制自动检测链：由固定值改为按 provider 的 `max_context_tokens` 自动探测。

### 0.1.2

- 初版公开版本。

---

## 📄 许可证

[GPL-3.0](LICENSE) © 2026 林 & 欧根亲王
