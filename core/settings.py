"""运行期设置（DB 存储，冻结）—— Web 控制台可改，worker 每轮读取。

与 core.config 的分工：config 是启动时定稿的静态配置；这里存运行时可切换项。
"""
from __future__ import annotations

from . import config
from .queue import db, init_db

DEFAULTS = {
    "backend": config.DEFAULT_BACKEND,      # x_api | browser
    "paused": "0",                          # 1 = 暂停出队
    "tweet_prefix": config.TWEET_PREFIX,
    "tweet_suffix": config.TWEET_SUFFIX,
    "quote_mode": config.QUOTE_MODE,
    "monthly_limit": str(config.MONTHLY_LIMIT),
    "dedup_window": str(config.DEDUP_WINDOW),
    # ── Telegram 机器人（Web 控制台可配，无需改 .env / 重启进程）──
    "tg_token": "",                          # 空 = 回落 .env 的 TG_TOKEN
    "tg_allowed_users": "",                  # 逗号分隔的 user id 白名单
    "tg_allowed_chats": "",                  # 逗号分隔的 chat id 白名单
    "tg_autostart": "1",                     # 1 = 启动时自动拉起机器人（默认开，可在控制台关掉）
    # ── X 浏览器后端的账号密码登录 ──
    "x_username": "",                        # 仅用于回显"上次登录账号"，不存密码
    # ── 队列调度（Web 控制台 / Telegram「设置」面板可改）──
    # "1" = 队列里的待发任务会被自动按间隔发出；
    # "0" = 只入库不发，必须手动点「🚀 发送」才发（适合想逐个把关的人）
    "queue_send_enabled": "1",
    # 两条推文之间**至少**间隔多少分钟（0 = 不限制，立即发下一条）。
    # 这是防连发被风控的软节流，不是精确调度。
    "send_interval_minutes": "0",
}

_TABLE = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _ensure() -> None:
    with db() as con:
        con.execute(_TABLE)


def all_settings() -> dict[str, str]:
    _ensure()
    with db() as con:
        stored = {r["key"]: r["value"] for r in con.execute("SELECT key,value FROM settings")}
    return {**DEFAULTS, **stored}


def get(key: str, default: str = "") -> str:
    return all_settings().get(key, default)


def get_int(key: str, default: int = 0) -> int:
    try:
        return int(get(key, str(default)))
    except ValueError:
        return default


def set_many(items: dict[str, str]) -> None:
    """批量更新。只接受 DEFAULTS 里已声明的键（防脏键写入）。"""
    from .queue import now_iso
    _ensure()
    ts = now_iso()
    with db() as con:
        for k, v in items.items():
            if k not in DEFAULTS:
                continue
            con.execute(
                "INSERT INTO settings (key,value,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (k, str(v), ts),
            )


def is_paused() -> bool:
    return get("paused", "0") == "1"


def current_backend() -> str:
    return get("backend", config.DEFAULT_BACKEND)


def effective_prefix() -> str:
    return get("tweet_prefix", config.TWEET_PREFIX)


def effective_suffix() -> str:
    return get("tweet_suffix", config.TWEET_SUFFIX)


def effective_dedup_window() -> int:
    return get_int("dedup_window", config.DEDUP_WINDOW)


def effective_monthly_limit() -> int:
    return get_int("monthly_limit", config.MONTHLY_LIMIT)


def queue_send_enabled() -> bool:
    """队列是否会自动发送待发任务。False = 只能手动逐条发。"""
    return get("queue_send_enabled", "1") == "1"


def effective_send_interval_seconds() -> int:
    """两条推文之间的最小间隔（秒）。0 = 不限制。"""
    try:
        minutes = get_int("send_interval_minutes", 0)
    except Exception:
        minutes = 0
    return max(0, int(minutes)) * 60
