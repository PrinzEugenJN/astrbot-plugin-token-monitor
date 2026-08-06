# Token 水位监控 · AstrBot 插件

<p align="center">
  <img src="logo.png" width="120" alt="欧根亲王 Logo"/>
  <br/>
  <em>Prinz Eugen Console · 银白 / 橙红 / 夜晚海面</em>
</p>

在 AstrBot Dashboard 中监控各会话的上下文 Token 水位，并在接近自动压缩阈值时主动推送警告。**由林与欧根亲王共同建造 ♡**

> 上下文上限 1,000,000 · 压缩阈值 82% · 警告阈值 75%（固定常量）

## ✨ 功能特性

- **会话水位总览**：卡片网格展示所有会话，点击切换焦点，主会话 ⭐ 星标
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
| `alert_enabled` | 警告总开关 | `true` |
| `alert_target_session` | 警告发送目标会话 | `default:FriendMessage:256418297` |
| `alert_scope` | 监控范围（main / all / custom） | `main` |
| `alert_custom_sessions` | 自定义会话 ID 列表（逗号分隔） | 空 |
| `alert_step_pct` | 越阈值后再次告警的水位上升百分比 | `5` |
| `check_interval_sec` | 后端水位检查间隔（秒） | `60` |
| `refresh_interval_ms` | 页面轮询间隔（毫秒） | `10000` |

## 📸 界面预览

（待补充截图）

## 🗂️ 目录结构

```text
astrbot_plugin_token_monitor/
├── main.py               # 插件入口：接口、告警巡检、压缩检测
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

## 📄 许可证

[GPL-3.0](LICENSE) © 2026 林 & 欧根亲王
