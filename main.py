from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections import defaultdict
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
MAIN_PLATFORM_ID = "default"
MAIN_USER_ID = "default:FriendMessage:256418297"
ALLOWED_STATS_DAYS = {1, 3, 7, 30}

# 部署结构为 core/data/plugins/<plugin>/main.py，因此 parents[2] 是 data。
DEPLOYMENT_DATABASE_FALLBACK = Path(
    r"C:\Users\欧根亲王\.astrbot_launcher\instances\831b358c-9138-4d35-a73c-bcbcca96e4b8\core\data\data_v4.db"
)


class TokenMonitorPlugin(Star):
    """Token 水位监控插件入口。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        self.config = config
        self._alert_task: asyncio.Task | None = None
        self._alert_state: dict[str, dict] = {}

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
            self.config.get(
                "alert_target_session",
                "default:FriendMessage:256418297",
            ),
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
                    if pref_row["scope_id"] == MAIN_USER_ID:
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
                and row["user_id"] == MAIN_USER_ID
                and row["conversation_id"] == current_main_conv
            ]

        alert_step_pct = self.config.get("alert_step_pct", 5)
        session_str = self.config.get(
            "alert_target_session", "default:FriendMessage:256418297"
        )
        context_limit = self._resolve_context_limit()
        compress_threshold_tokens = (
            context_limit * COMPRESS_THRESHOLD_PCT // 100
        )

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
            title = row["title"] or conversation_id[:8]
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

        context_limit = self._resolve_context_limit()
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
                current_main_conv = None
                for pref_row in connection.execute(
                    "SELECT scope_id, value FROM preferences WHERE key = 'sel_conv_id' AND scope = 'umo'"
                ).fetchall():
                    if pref_row["scope_id"] == MAIN_USER_ID:
                        try:
                            current_main_conv = json.loads(pref_row["value"]).get("val")
                        except (TypeError, ValueError):
                            current_main_conv = None
                        break

            conversations = []
            context_limit = self._resolve_context_limit()
            turn_config = self._resolve_turn_config()
            compress_threshold_tokens = (
                context_limit * COMPRESS_THRESHOLD_PCT // 100
            )
            for row in rows:
                token_usage = max(0, int(row["token_usage"] or 0))
                remaining_to_compress = max(
                    0, compress_threshold_tokens - token_usage
                )
                turns = self._count_turns(row["content"])
                conversations.append(
                    {
                        "conversation_id": row["conversation_id"],
                        "title": row["title"],
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
                            and row["user_id"] == MAIN_USER_ID
                            and (
                                current_main_conv is None
                                or row["conversation_id"] == current_main_conv
                            )
                        ),
                        "turns": turns,
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                    }
                )

            return json_response(
                {
                    "status": "ok",
                    "message": "",
                    "data": {
                        "context_limit": context_limit,
                        "compress_threshold_pct": COMPRESS_THRESHOLD_PCT,
                        "alert_threshold_pct": ALERT_THRESHOLD_PCT,
                        "turn_config": turn_config,
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

    def _resolve_context_limit(self) -> int:
        """上下文限制：插件配置 context_limit>0 优先，否则读 AstrBot provider 配置自动解析。"""
        cfg_limit = int(self.config.get("context_limit", 0) or 0)
        if cfg_limit > 0:
            return cfg_limit
        try:
            with open(self._cmd_config_path(), "r", encoding="utf-8-sig") as f:
                cmd = json.load(f)
            ps = cmd.get("provider_settings", {})
            default_id = ps.get("default_provider_id", "")
            fallback = int(ps.get("fallback_max_context_tokens", 0) or 128000)
            fallback = fallback if fallback > 0 else 128000
            for p in cmd.get("provider", []):
                if p.get("id") == default_id:
                    mct = int(p.get("max_context_tokens", 0) or 0)
                    return mct if mct > 0 else fallback
            return fallback
        except Exception as exc:
            logger.warning("读取上下文限制失败，使用默认 1M: %s", exc)
            return CONTEXT_LIMIT

    def _resolve_turn_config(self) -> dict:
        """轮数截断配置：策略 / 最大轮数 / 每次移除轮数。"""
        try:
            with open(self._cmd_config_path(), "r", encoding="utf-8-sig") as f:
                cmd = json.load(f)
            ps = cmd.get("provider_settings", {})
            return {
                "strategy": ps.get("context_limit_reached_strategy", "llm_compress"),
                "max_turns": int(ps.get("max_context_length", -1) or -1),
                "dequeue_turns": int(ps.get("dequeue_context_length", 10) or 10),
            }
        except Exception:
            return {"strategy": "llm_compress", "max_turns": -1, "dequeue_turns": 10}

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
        """会话展示名：title 优先，空则从 user_id 解析（群聊 → 群 xxx）。"""
        if title and str(title).strip():
            return str(title)
        parts = str(user_id or "").split(":")
        if len(parts) >= 3 and "Group" in parts[1]:
            return f"群 {parts[-1]}"
        if len(parts) >= 2:
            return parts[-1]
        return str(user_id or "未命名会话")

    @staticmethod
    def _database_path() -> Path:
        inferred_path = Path(__file__).resolve().parents[2] / "data_v4.db"
        if inferred_path.is_file():
            return inferred_path
        if DEPLOYMENT_DATABASE_FALLBACK.is_file():
            return DEPLOYMENT_DATABASE_FALLBACK
        raise FileNotFoundError(
            f"未找到 AstrBot 数据库（推算路径：{inferred_path}）"
        )

    @classmethod
    def _open_readonly_database(cls) -> sqlite3.Connection:
        database_path = cls._database_path()
        database_uri = f"file:{quote(database_path.as_posix(), safe='/:')}?mode=ro"
        connection = sqlite3.connect(database_uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection
