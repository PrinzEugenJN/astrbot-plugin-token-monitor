from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections import defaultdict
from datetime import datetime
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from astrbot.api import AstrBotConfig, logger
from astrbot.api.all import command
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request

CONTEXT_LIMIT = 1_000_000
COMPRESS_THRESHOLD_PCT = 82
ALERT_THRESHOLD_PCT = 75
DEFAULT_STALE_DAYS = 30
MAIN_PLATFORM_ID = "default"
ALLOWED_STATS_DAYS = {1, 3, 7, 30}

# 由 __init__ 从插件配置写入，避免把作者本机的会话标识打进源码。
# 留空时由 _resolve_main_user_id() 按 Token 用量最高的会话懒推断。
MAIN_USER_ID = ""
# AstrBot 核心数据库 data_v4.db 的绝对路径。
# 由 __init__ 向框架索取（BaseDatabase.DATABASE_URL），拿不到再按部署结构推断。
_CORE_DB_PATH = ""
# 主用户标识的推断缓存，避免每次判定都查库。
_MAIN_USER_CACHE: list[str | None] = [None]


class TokenMonitorPlugin(Star):
    """Token 水位监控插件入口。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        self.config = config
        self._alert_task: asyncio.Task | None = None
        self._alert_state: dict[str, dict] = {}

        global MAIN_USER_ID, _CORE_DB_PATH
        MAIN_USER_ID = str(config.get("main_user_id", "") or "").strip()
        _MAIN_USER_CACHE[0] = None
        _CORE_DB_PATH = self._resolve_core_db_path(context)
        logger.info(
            "Token 水位监控已加载｜主用户标识=%s｜核心数据库=%s",
            MAIN_USER_ID or "（留空，按用量最高会话推断）",
            _CORE_DB_PATH or "（未找到，请检查部署结构）",
        )

    async def initialize(self) -> None:
        """插件激活后注册只读接口并记录加载信息。"""
        self.context.register_web_api(
            "/token_monitor/conversations",
            self.get_conversations,
            ["GET"],
            "读取所有会话的 Token 水位",
        )
        self.context.register_web_api(
            "/token_monitor/stats/provider-tokens",
            self.get_provider_token_stats,
            ["GET"],
            "读取 Provider Token 小时趋势",
        )
        self.context.register_web_api(
            "/token_monitor/stats/alert-history",
            self.get_alert_history,
            ["GET"],
            "读取警告与压缩事件历史",
        )

        self._ensure_history_table()
        self._load_monitor_state()

        logger.info(
            "Token 水位监控插件已加载；警告启用=%s，目标会话=%s，"
            "检查间隔=%s 秒，页面刷新间隔=%s 毫秒",
            self.config.get("alert_enabled", True),
            self.config.get("alert_target_session", "") or "（未配置，不发送告警）",
            self.config.get("check_interval_sec", 60),
            self.config.get("refresh_interval_ms", 10000),
        )

        if self.config.get("alert_enabled", True):
            self._alert_task = asyncio.create_task(self._alert_loop())

    async def terminate(self) -> None:
        """插件卸载时停止水位警告任务。"""
        if self._alert_task is None:
            return
        self._alert_task.cancel()
        try:
            await self._alert_task
        except asyncio.CancelledError:
            pass
        finally:
            self._alert_task = None

    async def _alert_loop(self) -> None:
        """按配置间隔检查会话水位。"""
        while True:
            await asyncio.sleep(self.config.get("check_interval_sec", 60))
            try:
                await self._check_and_alert()
            except Exception as exc:
                logger.error("Token 水位警告检查失败: %s", exc)

    @staticmethod
    def _resolve_core_db_path(context) -> str:
        """向 AstrBot 框架索取核心数据库 data_v4.db 的真实路径。

        AstrBot 的 BaseDatabase 初始化时就把自己的 DATABASE_URL 存好了，
        形如 sqlite+aiosqlite:///C:/xxx/data_v4.db。直接读这个字段，
        比自己推断目录结构可靠得多——用户怎么部署都不影响。

        拿不到时才回落到部署结构推断；推断也失败就返回空串，
        由调用方给出可读的报错，而不是静默指向一个不存在的路径。
        """
        try:
            db = context.get_db()
            url = str(getattr(db, "DATABASE_URL", "") or "")
            if url:
                for prefix in (
                    "sqlite+aiosqlite:///",
                    "sqlite+aiosqlite://",
                    "sqlite:///",
                    "sqlite://",
                ):
                    if url.startswith(prefix):
                        raw = url[len(prefix):]
                        candidate = Path(raw).expanduser()
                        if candidate.is_file():
                            logger.info("核心数据库路径由框架提供：%s", candidate)
                            return str(candidate)
                        break
        except Exception as exc:
            logger.debug("向框架索取 DATABASE_URL 失败: %s", exc)

        base = Path(__file__).resolve().parents[2]
        for candidate in (
            base / "data_v4.db",
            base.parent / "data_v4.db",
            base.parent.parent / "data_v4.db",
        ):
            if candidate.is_file():
                logger.info("核心数据库路径由部署结构推断：%s", candidate)
                return str(candidate)

        logger.warning("未能定位 AstrBot 核心数据库 data_v4.db")
        return ""


    @staticmethod
    def _history_database_path() -> Path:
        """插件自己的历史数据库（plugin_data 目录，不碰 AstrBot 核心库）。"""
        base = Path(__file__).resolve().parents[2] / "plugin_data"
        plugin_dir = base / "astrbot_plugin_token_monitor"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        return plugin_dir / "alert_history.db"

    @classmethod
    def _open_history_database(cls) -> sqlite3.Connection:
        """打开插件历史库（可写连接，仅限本插件数据）。"""
        connection = sqlite3.connect(cls._history_database_path(), timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_history_table(self) -> None:
        """确保警告历史表与状态表存在（幂等）。"""
        try:
            with closing(self._open_history_database()) as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS alert_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at REAL NOT NULL,
                        conversation_id TEXT,
                        title TEXT,
                        event_type TEXT NOT NULL,
                        percent REAL,
                        token_usage INTEGER
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS monitor_state (
                        conversation_id TEXT PRIMARY KEY,
                        last_alert_pct REAL,
                        last_token_usage INTEGER,
                        last_turn_pct REAL,
                        updated_at REAL
                    )
                    """
                )
                # v0.1.2: 旧库迁移轮数告警状态列
                try:
                    connection.execute(
                        "ALTER TABLE monitor_state ADD COLUMN last_turn_pct REAL"
                    )
                except sqlite3.OperationalError:
                    pass  # 列已存在
                connection.commit()
        except Exception as exc:
            logger.error("初始化警告历史表失败: %s", exc)

    def _load_monitor_state(self) -> None:
        """从历史库恢复告警状态，避免重载/重启后压缩检测基线丢失。"""
        try:
            with closing(self._open_history_database()) as connection:
                rows = connection.execute(
                    "SELECT conversation_id, last_alert_pct, last_token_usage, last_turn_pct FROM monitor_state"
                ).fetchall()
            for row in rows:
                state = self._alert_state.setdefault(
                    row["conversation_id"],
                    {
                        "last_alert_pct": None,
                        "last_token_usage": None,
                        "last_turn_pct": None,
                    },
                )
                if row["last_alert_pct"] is not None:
                    state["last_alert_pct"] = row["last_alert_pct"]
                if row["last_token_usage"] is not None:
                    state["last_token_usage"] = row["last_token_usage"]
                if row["last_turn_pct"] is not None:
                    state["last_turn_pct"] = row["last_turn_pct"]
        except Exception as exc:
            logger.error("恢复告警状态失败: %s", exc)

    def _persist_monitor_state(self, conversation_id: str, state: dict) -> None:
        """将单个会话的告警状态写回历史库（失败不影响主流程）。"""
        try:
            with closing(self._open_history_database()) as connection:
                connection.execute(
                    """
                    INSERT INTO monitor_state
                        (conversation_id, last_alert_pct, last_token_usage, last_turn_pct, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(conversation_id) DO UPDATE SET
                        last_alert_pct = excluded.last_alert_pct,
                        last_token_usage = excluded.last_token_usage,
                        last_turn_pct = excluded.last_turn_pct,
                        updated_at = excluded.updated_at
                    """,
                    (
                        conversation_id,
                        state.get("last_alert_pct"),
                        state.get("last_token_usage"),
                        state.get("last_turn_pct"),
                        time.time(),
                    ),
                )
                connection.commit()
        except Exception as exc:
            logger.error("写入告警状态失败（会话 %s）: %s", conversation_id, exc)

    def _record_history(
        self,
        conversation_id: str,
        title: str,
        event_type: str,
        percent: float,
        token_usage: int,
    ) -> None:
        """写入一条警告/解除/压缩事件记录（失败不影响主流程）。"""
        try:
            with closing(self._open_history_database()) as connection:
                connection.execute(
                    """
                    INSERT INTO alert_history
                        (created_at, conversation_id, title, event_type, percent, token_usage)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        time.time(),
                        conversation_id,
                        title,
                        event_type,
                        percent,
                        token_usage,
                    ),
                )
                connection.commit()
        except Exception as exc:
            logger.error("写入警告历史失败: %s", exc)

    async def get_alert_history(self):
        """返回最近警告/解除/压缩事件（可选按会话过滤）。"""
        try:
            raw_limit = request.query.get("limit", "20")
            try:
                limit = min(100, max(1, int(raw_limit)))
            except (TypeError, ValueError):
                limit = 20
            conversation_id = request.query.get("conversation_id", "").strip()

            with closing(self._open_history_database()) as connection:
                if conversation_id:
                    rows = connection.execute(
                        "SELECT * FROM alert_history WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
                        (conversation_id, limit),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        "SELECT * FROM alert_history ORDER BY id DESC LIMIT ?",
                        (limit,),
                    ).fetchall()

            history = [
                {
                    "id": r["id"],
                    "created_at": r["created_at"],
                    "conversation_id": r["conversation_id"],
                    "title": r["title"],
                    "event_type": r["event_type"],
                    "percent": r["percent"],
                    "token_usage": r["token_usage"],
                }
                for r in rows
            ]
            return json_response(
                {"status": "ok", "message": "", "data": {"count": len(history), "history": history}}
            )
        except (OSError, sqlite3.Error, ValueError) as exc:
            logger.error("Token 警告历史接口读取失败: %s", exc)
            return error_response(
                "警告历史数据暂不可用，请稍后重试",
                status_code=503,
                data={"code": "database_unavailable"},
            )

    async def _check_and_alert(self) -> None:
        """筛选监控会话，并按水位台阶发送警告或解除消息。"""
        alert_scope = self.config.get("alert_scope", "main")
        custom_sessions = {
            conversation_id.strip()
            for conversation_id in self.config.get(
                "alert_custom_sessions", ""
            ).split(",")
            if conversation_id.strip()
        }

        with closing(self._open_readonly_database()) as connection:
            rows = connection.execute(
                """
                SELECT
                    conversation_id,
                    title,
                    platform_id,
                    user_id,
                    token_usage,
                    content
                FROM conversations
                """
            ).fetchall()

            current_main_conv = None
            if alert_scope == "main":
                for pref_row in connection.execute(
                    "SELECT scope_id, value FROM preferences WHERE key = 'sel_conv_id' AND scope = 'umo'"
                ).fetchall():
                    if pref_row["scope_id"] == self._resolve_main_user_id():
                        try:
                            current_main_conv = json.loads(pref_row["value"]).get(
                                "val"
                            )
                        except (TypeError, ValueError):
                            current_main_conv = None
                        break

                # 兜底：偏好缺失时回退到该用户最近更新的会话，避免告警静默失效
                if current_main_conv is None:
                    fallback_row = connection.execute(
                        """
                        SELECT conversation_id FROM conversations
                        WHERE platform_id = ? AND user_id = ?
                        ORDER BY updated_at DESC LIMIT 1
                        """,
                        (MAIN_PLATFORM_ID, MAIN_USER_ID),
                    ).fetchone()
                    if fallback_row:
                        current_main_conv = fallback_row["conversation_id"]
                        logger.warning(
                            "未找到主会话偏好(sel_conv_id)，已回退到最新会话 %s",
                            current_main_conv,
                        )

        if alert_scope == "all":
            monitored_rows = rows
        elif alert_scope == "custom":
            monitored_rows = [
                row
                for row in rows
                if row["conversation_id"] in custom_sessions
            ]
        else:
            monitored_rows = [
                row
                for row in rows
                if row["platform_id"] == MAIN_PLATFORM_ID
                and row["user_id"] == self._resolve_main_user_id()
                and row["conversation_id"] == current_main_conv
            ]

        alert_step_pct = self.config.get("alert_step_pct", 5)
        session_str = str(self.config.get("alert_target_session", "") or "").strip()
        if not session_str:
            # 未配置告警目标：跳过推送，只在页面展示水位
            logger.debug("未配置 alert_target_session，本次跳过告警推送")
            return
        # 轮数告警配置（v0.1.2）
        turn_config = self._resolve_turn_config()
        turn_alert_enabled = bool(self.config.get("turn_alert_enabled", True))
        turn_alert_pct = int(self.config.get("alert_turn_pct", 75))
        turn_step_pct = int(self.config.get("turn_step_pct", 5))

        for row in monitored_rows:
            token_usage = row["token_usage"]
            if token_usage is None or token_usage <= 0:
                continue

            conversation_id = row["conversation_id"]
            # 展示名：title 为空时从 user_id 解析，避免历史记录里出现裸 ID
            title = self._resolve_display_name(row["title"], row["user_id"])
            # 按该会话实际生效的 provider 计算上限（跟随会话级热切换）
            context_limit = self._resolve_context_limit(
                row["user_id"], str(row["conversation_id"] or "")
            )
            compress_threshold_tokens = (
                context_limit * COMPRESS_THRESHOLD_PCT // 100
            )
            percent = round(token_usage / context_limit * 100, 2)
            state = self._alert_state.setdefault(
                conversation_id,
                {"last_alert_pct": None, "last_token_usage": None},
            )
            last_alert_pct = state["last_alert_pct"]

            if percent >= ALERT_THRESHOLD_PCT and (
                last_alert_pct is None
                or percent >= last_alert_pct + alert_step_pct
            ):
                remaining = max(0, compress_threshold_tokens - token_usage)
                text = (
                    f"⚠️ Token 水位警告：{title} 当前 {percent}%"
                    f"（{token_usage:,}/{context_limit:,}），距压缩阈值 "
                    f"{COMPRESS_THRESHOLD_PCT}% 还差 {remaining:,} tokens"
                )
                try:
                    await self.context.send_message(
                        session_str, MessageChain().message(text)
                    )
                except Exception as exc:
                    logger.error(
                        "Token 水位警告发送失败（会话 %s）: %s",
                        conversation_id,
                        exc,
                    )
                else:
                    state["last_alert_pct"] = percent
                    self._record_history(
                        conversation_id, title, "warning", percent, token_usage
                    )
            elif last_alert_pct is not None and percent < ALERT_THRESHOLD_PCT:
                text = (
                    f"✅ Token 水位警告已解除：{title} 当前 {percent}%"
                    f"（{token_usage:,}/{context_limit:,}），低于警告阈值 "
                    f"{ALERT_THRESHOLD_PCT}%"
                )
                try:
                    await self.context.send_message(
                        session_str, MessageChain().message(text)
                    )
                except Exception as exc:
                    logger.error(
                        "Token 水位警告解除消息发送失败（会话 %s）: %s",
                        conversation_id,
                        exc,
                    )
                else:
                    state["last_alert_pct"] = None
                    self._record_history(
                        conversation_id, title, "cleared", percent, token_usage
                    )

            # 压缩/回落检测：水位骤降视为压缩事件，中等降幅视为清理回落
            last_usage = state.get("last_token_usage")
            if (
                last_usage is not None
                and last_usage > context_limit * 30 // 100
            ):
                if token_usage < last_usage * 0.6:
                    self._record_history(
                        conversation_id, title, "compressed", percent, token_usage
                    )
                elif token_usage < last_usage * 0.85:
                    self._record_history(
                        conversation_id, title, "rollback", percent, token_usage
                    )
            # 轮数告警（v0.1.2）：truncate_by_turns 策略下按轮数占比告警
            if (
                turn_alert_enabled
                and turn_config["strategy"] == "truncate_by_turns"
                and turn_config["max_turns"] > 0
            ):
                turns = self._count_turns(row["content"])
                max_turns = turn_config["max_turns"]
                if turns > 0:
                    turn_pct = round(turns / max_turns * 100, 2)
                    last_turn_pct = state.get("last_turn_pct")
                    if turn_pct >= turn_alert_pct and (
                        last_turn_pct is None
                        or turn_pct >= last_turn_pct + turn_step_pct
                    ):
                        remaining_turns = max(0, max_turns - turns)
                        text = (
                            f"轮数警告：{title} 当前 {turns}/{max_turns} 轮"
                            f"（{turn_pct}%），还剩 {remaining_turns} 轮将被截断"
                        )
                        try:
                            await self.context.send_message(
                                session_str, MessageChain().message(text)
                            )
                        except Exception as exc:
                            logger.error(
                                "轮数警告发送失败（会话 %s）: %s",
                                conversation_id,
                                exc,
                            )
                        else:
                            state["last_turn_pct"] = turn_pct
                            self._record_history(
                                conversation_id,
                                title,
                                "turn_warning",
                                turn_pct,
                                turns,
                            )

            state["last_token_usage"] = token_usage
            self._persist_monitor_state(conversation_id, state)

    @command("ctx")
    async def ctx_cmd(self, event: AstrMessageEvent):
        """查询当前会话 Token 水位状态。用法：/ctx [history [N]]"""
        target = str(event.session)
        parts = event.message_str.strip().split()
        sub = parts[1].lower() if len(parts) > 1 else "status"

        async def reply(text: str) -> None:
            await self.context.send_message(
                target, MessageChain().message(text)
            )

        if sub == "help":
            await reply(
                "用法：/ctx 查看当前会话水位；"
                "/ctx history [N] 查看最近 N 条告警历史（默认 5）；"
                "/ctx help 帮助"
            )
            event.stop_event()
            return

        if sub == "history":
            n = 5
            if len(parts) > 2 and parts[2].isdigit():
                n = min(int(parts[2]), 50)
            try:
                with closing(self._open_history_database()) as connection:
                    rows = connection.execute(
                        """
                        SELECT created_at, title, event_type, percent, token_usage
                        FROM alert_history
                        ORDER BY id DESC LIMIT ?
                        """,
                        (n,),
                    ).fetchall()
            except Exception as exc:
                logger.error("查询告警历史失败: %s", exc)
                rows = []
            if not rows:
                await reply("暂无告警历史。")
                event.stop_event()
                return
            lines = ["最近告警历史："]
            for row in rows:
                ts = time.strftime(
                    "%m-%d %H:%M", time.localtime(row["created_at"])
                )
                lines.append(
                    f"{ts} [{row['event_type']}] {row['title']} "
                    f"{row['percent']}%"
                )
            await reply("\n".join(lines))
            event.stop_event()
            return

        # status：查询当前会话（platform_id + unified_msg_origin 匹配）
        try:
            with closing(self._open_readonly_database()) as connection:
                row = connection.execute(
                    """
                    SELECT conversation_id, title, token_usage, content
                    FROM conversations
                    WHERE platform_id = ? AND user_id = ?
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    (event.platform_meta.id, str(event.session)),
                ).fetchone()
        except Exception as exc:
            logger.error("查询当前会话水位失败: %s", exc)
            row = None
        if not row:
            await reply("未找到当前会话的水位数据。")
            event.stop_event()
            return

        context_limit = self._resolve_context_limit(
            str(event.session), str(row["conversation_id"] or "")
        )
        token_usage = row["token_usage"] or 0
        percent = round(token_usage / context_limit * 100, 2)
        turn_config = self._resolve_turn_config()
        lines = [
            f"会话：{row['title'] or row['conversation_id'][:8]}",
            f"Token：{token_usage:,} / {context_limit:,}（{percent}%）",
            f"策略：{turn_config['strategy']}",
        ]
        if (
            turn_config["strategy"] == "truncate_by_turns"
            and turn_config["max_turns"] > 0
        ):
            turns = self._count_turns(row["content"])
            turn_pct = round(turns / turn_config["max_turns"] * 100, 2)
            lines.append(
                f"轮数：{turns} / {turn_config['max_turns']}（{turn_pct}%）"
            )
        await reply("\n".join(lines))
        event.stop_event()

    async def get_conversations(self):
        """返回全部会话水位；百分比及阈值余量由后端统一计算。"""
        try:
            with closing(self._open_readonly_database()) as connection:
                rows = connection.execute(
                    """
                    SELECT
                        conversation_id,
                        title,
                        platform_id,
                        user_id,
                        content,
                        COALESCE(token_usage, 0) AS token_usage,
                        created_at,
                    updated_at
                    FROM conversations
                    ORDER BY COALESCE(token_usage, 0) DESC, updated_at DESC
                    """
                ).fetchall()

                # 主会话判定：以 preferences 中当前选中的会话（sel_conv_id）为准，
                # 避免多个同用户会话时误选历史僵尸会话。必须在连接关闭前查询。
                # 同步收集所有 user 的当前会话，用于通用 status 判定（不写死任何 ID）。
                active_by_scope: dict[str, str] = {}
                current_main_conv = None
                for pref_row in connection.execute(
                    "SELECT scope_id, value FROM preferences WHERE key = 'sel_conv_id' AND scope = 'umo'"
                ).fetchall():
                    try:
                        sel_val = json.loads(pref_row["value"]).get("val")
                    except (TypeError, ValueError):
                        sel_val = None
                    if sel_val:
                        active_by_scope[pref_row["scope_id"]] = sel_val
                    if pref_row["scope_id"] == self._resolve_main_user_id():
                        current_main_conv = sel_val

            conversations = []
            stale_days = int(self.config.get("stale_days", DEFAULT_STALE_DAYS) or 0)
            turn_config = self._resolve_turn_config()
            main_context_limit = None
            main_conv_id = ""

            # 一次性取每个会话最近一次真实调用的 provider，避免循环里逐个查库
            latest_provider: dict[str, str] = {}
            try:
                with closing(self._open_readonly_database()) as pconn:
                    for prow in pconn.execute(
                        "SELECT ps.conversation_id AS cid, ps.provider_id AS pid "
                        "FROM provider_stats ps "
                        "INNER JOIN ("
                        "  SELECT conversation_id, MAX(start_time) AS mt "
                        "  FROM provider_stats GROUP BY conversation_id"
                        ") t ON ps.conversation_id = t.conversation_id "
                        "   AND ps.start_time = t.mt"
                    ).fetchall():
                        if prow["cid"]:
                            latest_provider[str(prow["cid"])] = str(prow["pid"] or "")
            except Exception as exc:
                logger.debug("批量读取会话最近 provider 失败: %s", exc)

            # provider_id → model 名 的映射（用于卡片上显示短名）
            provider_models: dict[str, str] = {}
            try:
                with open(self._cmd_config_path(), "r", encoding="utf-8-sig") as f:
                    _cfg = json.load(f)
                for _p in (_cfg.get("provider", []) or []):
                    _pid = str(_p.get("id", "") or "")
                    if _pid:
                        provider_models[_pid] = str(_p.get("model", "") or "")
            except Exception as exc:
                logger.debug("读取 provider 模型名映射失败: %s", exc)

            for row in rows:
                token_usage = max(0, int(row["token_usage"] or 0))
                # 按该会话实际生效的 provider 计算上限（跟随会话级热切换）
                context_limit = self._resolve_context_limit(
                    row["user_id"], str(row["conversation_id"] or "")
                )
                compress_threshold_tokens = (
                    context_limit * COMPRESS_THRESHOLD_PCT // 100
                )
                # 记录主会话的真实上限，供顶层 data.context_limit 返回
                if row["user_id"] == self._resolve_main_user_id() and (
                    current_main_conv is None
                    or row["conversation_id"] == current_main_conv
                ):
                    main_context_limit = context_limit
                    main_conv_id = str(row["conversation_id"] or "")
                remaining_to_compress = max(
                    0, compress_threshold_tokens - token_usage
                )
                turns = self._count_turns(row["content"])
                active_conv = active_by_scope.get(row["user_id"])
                if row["conversation_id"] == active_conv:
                    session_status = "active"
                elif stale_days > 0 and self._stale_age_days(row["updated_at"]) > stale_days:
                    session_status = "stale"
                else:
                    session_status = "idle"
                conversations.append(
                    {
                        "conversation_id": row["conversation_id"],
                        "title": row["title"],
                        "status": session_status,
                        "display_name": self._resolve_display_name(
                            row["title"], row["user_id"]
                        ),
                        "platform_id": row["platform_id"],
                        "user_id": row["user_id"],
                        "token_usage": token_usage,
                        "percent": round(token_usage / context_limit * 100, 2),
                        "remaining_to_compress": remaining_to_compress,
                        "over_compress_threshold": (
                            token_usage >= compress_threshold_tokens
                        ),
                        "alerting": (
                            token_usage
                            >= context_limit * ALERT_THRESHOLD_PCT // 100
                        ),
                        "is_main": (
                            row["platform_id"] == MAIN_PLATFORM_ID
                            and row["user_id"] == self._resolve_main_user_id()
                            and (
                                current_main_conv is None
                                or row["conversation_id"] == current_main_conv
                            )
                        ),
                        "turns": turns,
                        "provider_id": latest_provider.get(
                            str(row["conversation_id"] or ""), ""
                        ),
                        "model_name": provider_models.get(
                            latest_provider.get(str(row["conversation_id"] or ""), ""),
                            "",
                        ),
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                    }
                )

            return json_response(
                {
                    "status": "ok",
                    "message": "",
                    "data": {
                        "context_limit": main_context_limit
                        or self._resolve_context_limit(
                            self._resolve_main_user_id(), str(main_conv_id or "")
                        ),
                        "compress_threshold_pct": COMPRESS_THRESHOLD_PCT,
                        "alert_threshold_pct": ALERT_THRESHOLD_PCT,
                        "turn_config": turn_config,
                        "stale_days": stale_days,
                        "count": len(conversations),
                        "conversations": conversations,
                    },
                }
            )
        except (OSError, sqlite3.Error, ValueError) as exc:
            logger.error("Token 水位会话接口读取失败: %s", exc)
            return error_response(
                "会话水位数据暂不可用，请稍后重试",
                status_code=503,
                data={"code": "database_unavailable"},
            )

    async def get_provider_token_stats(self):
        """按小时聚合 Provider Token，用量口径与核心统计接口一致。"""
        raw_days = request.query.get("days", "1")
        try:
            days = int(raw_days)
        except (TypeError, ValueError):
            days = 0
        if days not in ALLOWED_STATS_DAYS:
            return error_response(
                "days 仅支持 1、3、7 或 30",
                status_code=400,
                data={"code": "invalid_days", "allowed": [1, 3, 7]},
            )

        try:
            now = time.time()
            range_start = now - days * 24 * 60 * 60
            first_bucket = int(range_start // 3600) * 3600
            last_bucket = int(now // 3600) * 3600
            conversation_id = request.query.get("conversation_id", "").strip()

            with closing(self._open_readonly_database()) as connection:
                sql = """
                    SELECT
                        CAST(start_time / 3600 AS INTEGER) * 3600 AS bucket,
                        COALESCE(NULLIF(provider_id, ''), 'unknown') AS provider_id,
                        SUM(
                            COALESCE(token_input_other, 0)
                            + COALESCE(token_input_cached, 0)
                            + COALESCE(token_output, 0)
                        ) AS tokens,
                        COUNT(*) AS calls
                    FROM provider_stats
                    WHERE agent_type = 'internal'
                        AND start_time >= ?
                        AND start_time <= ?
                """
                params: list = [range_start, now]
                if conversation_id:
                    sql += " AND conversation_id = ?"
                    params.append(conversation_id)
                sql += (
                    " GROUP BY bucket, provider_id"
                    " ORDER BY bucket ASC, provider_id ASC"
                )
                rows = connection.execute(sql, params).fetchall()

            bucket_tokens: dict[int, int] = defaultdict(int)
            provider_tokens: dict[str, int] = defaultdict(int)
            provider_calls: dict[str, int] = defaultdict(int)
            provider_buckets: dict[str, dict[int, int]] = defaultdict(
                lambda: defaultdict(int)
            )

            for row in rows:
                bucket = int(row["bucket"])
                provider_id = str(row["provider_id"])
                tokens = int(row["tokens"] or 0)
                calls = int(row["calls"] or 0)
                bucket_tokens[bucket] += tokens
                provider_tokens[provider_id] += tokens
                provider_calls[provider_id] += calls
                provider_buckets[provider_id][bucket] += tokens

            bucket_starts = list(range(first_bucket, last_bucket + 1, 3600))
            total_series = [
                [bucket * 1000, bucket_tokens.get(bucket, 0)]
                for bucket in bucket_starts
            ]
            sorted_providers = sorted(
                provider_tokens,
                key=lambda provider_id: provider_tokens[provider_id],
                reverse=True,
            )
            series = [
                {
                    "name": provider_id,
                    "data": [
                        [bucket * 1000, provider_buckets[provider_id].get(bucket, 0)]
                        for bucket in bucket_starts
                    ],
                    "total_tokens": provider_tokens[provider_id],
                }
                for provider_id in sorted_providers
            ]
            total = [
                {
                    "provider_id": provider_id,
                    "tokens": provider_tokens[provider_id],
                    "calls": provider_calls[provider_id],
                }
                for provider_id in sorted_providers
            ]

            return json_response(
                {
                    "status": "ok",
                    "message": "",
                    "data": {
                        "days": days,
                        "range_start": int(range_start * 1000),
                        "range_end": int(now * 1000),
                        "trend": {
                            "series": series,
                            "total_series": total_series,
                        },
                        "total": total,
                        "range_total_tokens": sum(provider_tokens.values()),
                        "range_total_calls": sum(provider_calls.values()),
                    },
                }
            )
        except (OSError, sqlite3.Error, ValueError) as exc:
            logger.error("Token 水位趋势接口读取失败: %s", exc)
            return error_response(
                "Token 趋势数据暂不可用，请稍后重试",
                status_code=503,
                data={"code": "database_unavailable"},
            )

    @staticmethod
    def _cmd_config_path() -> Path:
        """AstrBot 主配置文件路径（插件部署目录 parents[2] 为 data/）"""
        return Path(__file__).resolve().parents[2] / "cmd_config.json"

    def _resolve_context_limit(
        self, umo: str | None = None, conversation_id: str = ""
    ) -> int:
        """上下文限制（自动探测，不写死任何模型值）。

        判定顺序（不依赖任何第三方插件，装没装热切换都能用）：
        ① 插件自身 context_limit（用户显式指定）
        ② provider_stats 表：该会话最近一次真实调用用的 provider_id
           —— AstrBot 核心每次调用后都会写这条记录，是最可信的实况，
              无论模型是通过热切换插件、/provider 命令还是改全局配置换的
        ③ preferences 表：会话级 provider_perf_chat_completion
           —— 装了热切换插件或用过 /provider 命令才有，作为补充
        ④ 全局默认 provider（agent_runner.config.model.provider_id）
        ⑤ LLM_METADATAS 模型默认
        ⑥ compression.fallback_max_tokens
        ⑦ 内置 128000

        所有取值都来自配置文件或数据库，不读任何内存快照，避免
        「运行期改过 max_context_tokens 但读到的还是旧值」。
        """
        cfg_limit = int(self.config.get("context_limit", 0) or 0)
        if cfg_limit > 0:
            return cfg_limit

        # ② 从 provider_stats 取该会话最近一次真实调用的 provider_id
        stats_provider_id = ""
        if conversation_id:
            try:
                with closing(self._open_readonly_database()) as conn:
                    row = conn.execute(
                        "SELECT provider_id FROM provider_stats "
                        "WHERE conversation_id = ? "
                        "AND COALESCE(provider_id, '') <> '' "
                        "ORDER BY start_time DESC LIMIT 1",
                        (conversation_id,),
                    ).fetchone()
                    if row is not None:
                        stats_provider_id = str(row["provider_id"]).strip()
            except Exception as exc:
                logger.debug("从 provider_stats 解析 provider 失败: %s", exc)

        # ③ preferences 表（兼容热切换插件 / /provider 命令）
        pref_provider_id = ""
        if umo:
            try:
                with closing(self._open_readonly_database()) as conn:
                    row = conn.execute(
                        "SELECT value FROM preferences "
                        "WHERE scope = 'umo' AND scope_id = ? AND key = ?",
                        (umo, "provider_perf_chat_completion"),
                    ).fetchone()
                    if row is not None and row["value"]:
                        raw = str(row["value"])
                        try:
                            parsed = json.loads(raw)
                            pref_provider_id = str(
                                (parsed.get("val") if isinstance(parsed, dict) else parsed)
                                or ""
                            ).strip()
                        except Exception:
                            pref_provider_id = raw.strip()
            except Exception as exc:
                logger.debug("读取会话级 provider 失败: %s", exc)

        # ④~⑦ 读配置文件
        try:
            with open(self._cmd_config_path(), "r", encoding="utf-8-sig") as f:
                cmd = json.load(f)
            providers = cmd.get("provider", []) or []

            def _mct_by_id(pid: str) -> int:
                if not pid:
                    return 0
                cfg = next((p for p in providers if p.get("id") == pid), None)
                if cfg is None:
                    return 0
                return int(cfg.get("max_context_tokens", 0) or 0)

            # 实况优先：provider_stats → preferences
            for pid in (stats_provider_id, pref_provider_id):
                mct = _mct_by_id(pid)
                if mct > 0:
                    if pid == stats_provider_id and pid:
                        logger.debug("上下文上限取自实况记录：%s → %s", pid, mct)
                    return mct

            # ④ 全局默认
            ar_cfg = (cmd.get("agent_runner") or {}).get("config") or {}
            global_id = str((ar_cfg.get("model") or {}).get("provider_id") or "").strip()
            mct = _mct_by_id(global_id)
            if mct > 0:
                return mct

            # ⑤ LLM_METADATAS
            if global_id:
                prov_cfg = next(
                    (p for p in providers if p.get("id") == global_id), None
                )
                model = (prov_cfg or {}).get("model") or ""
                if model:
                    try:
                        # 注：这是 AstrBot 内部模块（非 astrbot.api），
                        # 版本升级可能挪动位置。已包 try/except，取不到就跳过。
                        from astrbot.core.utils.llm_metadata import LLM_METADATAS
                        info = LLM_METADATAS.get(model)
                        if (
                            info
                            and info.get("limit")
                            and int(info["limit"].get("context", 0) or 0) > 0
                        ):
                            return int(info["limit"]["context"])
                    except Exception:
                        pass

            # ⑥ fallback
            fallback = int(
                ((ar_cfg.get("compression") or {}).get("fallback_max_tokens", 0)) or 0
            )
            if fallback > 0:
                return fallback
            return 128000
        except Exception as exc:
            logger.warning("读取上下文限制失败，使用默认 1M: %s", exc)
            return CONTEXT_LIMIT
    def _resolve_turn_config(self) -> dict:
        """轮数截断配置：策略 / 最大轮数 / 每次移除轮数。

        2026-11-16 重构：AstrBot 已把压缩相关项从 provider_settings 迁到
        agent_runner.config.compression，旧位置恒为 null，导致策略永远显示默认值。
        字段映射：overflow_strategy → strategy，max_turns → max_turns，
        trim_turns → dequeue_turns。
        """
        default = {"strategy": "llm_compress", "max_turns": -1, "dequeue_turns": 10}
        try:
            with open(self._cmd_config_path(), "r", encoding="utf-8-sig") as f:
                cmd = json.load(f)
            ar_cfg = ((cmd.get("agent_runner") or {}).get("config")) or {}
            comp = ar_cfg.get("compression") or {}
            if comp:
                return {
                    "strategy": str(comp.get("overflow_strategy") or "llm_compress"),
                    "max_turns": int(comp.get("max_turns", -1) or -1),
                    "dequeue_turns": int(comp.get("trim_turns", 10) or 10),
                }
            # 老版本兜底：仍读 provider_settings
            ps = cmd.get("provider_settings") or {}
            return {
                "strategy": ps.get("context_limit_reached_strategy", "llm_compress"),
                "max_turns": int(ps.get("max_context_length", -1) or -1),
                "dequeue_turns": int(ps.get("dequeue_context_length", 10) or 10),
            }
        except Exception:
            return default

    @staticmethod
    def _stale_age_days(updated_at: str) -> int:
        """会话距今冻结天数（UTC），解析失败返回 0。"""
        try:
            dt = datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S.%f")
        except (TypeError, ValueError):
            try:
                dt = datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                return 0
        return (datetime.utcnow() - dt).days

    @staticmethod
    def _count_turns(content) -> int:
        """从会话 content 中统计轮数（user 消息条数）。"""
        if not content:
            return 0
        try:
            messages = json.loads(content)
            if not isinstance(messages, list):
                return 0
            return sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "user")
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _resolve_display_name(title, user_id):
        """会话展示名：title 优先，空则从 user_id 解析。

        2026-11-16 增强：AstrBot 的 conversations.title 常年为 NULL，
        旧实现会一路退化到裸 QQ 号，导致告警历史里标题全是数字、无法对号。
        这里改为按会话类型给出可读标签，并附带可辨识的短后缀。
        """
        if title and str(title).strip():
            return str(title)

        raw = str(user_id or "").strip()
        if not raw:
            return "未命名会话"

        parts = raw.split(":")
        # 形如 default:FriendMessage:<QQ> / default:GroupMessage:<群号>
        if len(parts) >= 3:
            msg_type, ident = parts[1], parts[-1]
            if "Group" in msg_type:
                return f"群 {ident}"
            if "Friend" in msg_type:
                return f"私聊 {ident}"
            if "Guild" in msg_type or "Channel" in msg_type:
                return f"频道 {ident}"
            return f"{msg_type} {ident}"
        if len(parts) == 2:
            return parts[-1]
        return raw

    @staticmethod
    def _resolve_main_user_id() -> str:
        """返回「主用户」的会话标识（形如 default:FriendMessage:<QQ>）。

        优先级：
        ① 插件配置 main_user_id（用户显式填写，最可信）
        ② 自动推断：platform_id 匹配且 token_usage 最高的那条会话的 user_id

        自动推断在只有一个活跃会话时是准的；一旦有多个（换过号、旧会话残留），
        就可能判错，把告警发到不该发的地方。所以配置项说明里已明确建议显式填写。
        """
        if MAIN_USER_ID:
            return MAIN_USER_ID
        if _MAIN_USER_CACHE[0]:
            return _MAIN_USER_CACHE[0]
        try:
            with closing(TokenMonitorPlugin._open_readonly_database()) as connection:
                row = connection.execute(
                    "SELECT user_id FROM conversations "
                    "WHERE platform_id = ? AND COALESCE(user_id, '') <> '' "
                    "ORDER BY COALESCE(token_usage, 0) DESC LIMIT 1",
                    (MAIN_PLATFORM_ID,),
                ).fetchone()
                if row is not None and row["user_id"]:
                    _MAIN_USER_CACHE[0] = str(row["user_id"])
                    logger.info("未配置主用户标识，已按用量推断为：%s", _MAIN_USER_CACHE[0])
                    return _MAIN_USER_CACHE[0]
        except Exception as exc:
            logger.debug("推断主用户标识失败: %s", exc)
        return ""


    @staticmethod
    def _database_path() -> Path:
        """定位 AstrBot 核心数据库 data_v4.db。

        优先级：
        ① _CORE_DB_PATH —— 由 __init__ 向框架索取（BaseDatabase.DATABASE_URL），
           这是最可靠的一档，与用户怎么部署无关
        ② 部署结构推断：core/data/plugins/<plugin>/main.py → parents[2] 即 core/data
        ③ 再向上两级兜底

        全部失败时抛错，并把试过的路径一并列出，便于用户排查。
        """
        tried: list[str] = []

        if _CORE_DB_PATH:
            cached = Path(_CORE_DB_PATH)
            if cached.is_file():
                return cached
            tried.append(f"{cached}（框架提供的路径）")

        base = Path(__file__).resolve().parents[2]
        for candidate in (
            base / "data_v4.db",
            base.parent / "data_v4.db",
            base.parent.parent / "data_v4.db",
        ):
            tried.append(str(candidate))
            if candidate.is_file():
                return candidate

        detail = "；".join(tried) if tried else "（无）"
        raise FileNotFoundError(
            f"未找到 AstrBot 核心数据库 data_v4.db。已尝试以下位置：{detail}。"
            "本插件会优先向 AstrBot 框架索取数据库路径，若此处仍然报错，"
            "通常意味着框架接口与插件版本不匹配，请提交 Issue 并附上本段报错。"
        )
    @classmethod
    def _open_readonly_database(cls) -> sqlite3.Connection:
        database_path = cls._database_path()
        database_uri = f"file:{quote(database_path.as_posix(), safe='/:')}?mode=ro"
        connection = sqlite3.connect(database_uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection
