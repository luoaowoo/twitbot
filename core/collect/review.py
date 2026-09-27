"""待审核队列：每小时挑 1 条推给 TG 管理员，通过才发（新文件）。

用户定的流程（2026-09-27）：
    每小时 → 从采集库挑 1 条（未处理 + 打分最高）
           → 推给管理员（带图 + 热评 + 按钮）
           → 管理员点「✅ 通过并发送」才真的发到 X
           → 或「⏭ 跳过」标记掉

**绝不自动发** —— 用户明确说「不要直接发」。
配置写在自己的 review_config 表（core/settings.py 是冻结的白名单制）。
"""
from __future__ import annotations

import logging
import time

from .. import queue
from . import store

log = logging.getLogger("twitbot.collect.review")

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_queue (
    key        TEXT PRIMARY KEY,
    state      TEXT NOT NULL DEFAULT 'pending',   -- pending|approved|skipped
    pushed_at  TEXT DEFAULT '',
    decided_at TEXT DEFAULT '',
    message_id INTEGER DEFAULT 0,                 -- TG 消息 id，用来改卡片
    chat_id    INTEGER DEFAULT 0,
    job_id     INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS review_config (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""

DEFAULTS = {
    "enabled": "0",              # 默认关！用户开才跑
    "interval_minutes": "60",    # 每小时一条
    "min_score": "60",           # 低于这个分不推
}


def init_db() -> None:
    with queue.db() as con:
        con.executescript(SCHEMA)


def get_conf(key: str, default: str = "") -> str:
    try:
        init_db()
        with queue.db() as con:
            r = con.execute("SELECT value FROM review_config WHERE key=?", (key,)).fetchone()
        return r["value"] if r else DEFAULTS.get(key, default)
    except Exception:
        return DEFAULTS.get(key, default)


def set_conf(items: dict) -> None:
    try:
        init_db()
        ts = queue.now_iso()
        with queue.db() as con:
            for k, v in items.items():
                if k not in DEFAULTS:
                    continue
                con.execute(
                    "INSERT INTO review_config (key,value,updated_at) VALUES (?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "updated_at=excluded.updated_at", (k, str(v), ts))
    except Exception as e:
        log.debug("写 review_config 失败: %s", e)


def conf_all() -> dict:
    return {k: get_conf(k) for k in DEFAULTS}


def next_candidate(min_score: int | None = None) -> dict | None:
    """挑下一条待审：没进过审核队列 + 没被硬门槛拒 + 分数够。"""
    try:
        init_db()
        ms = int(min_score if min_score is not None else get_conf("min_score", "60"))
        with queue.db() as con:
            r = con.execute(
                "SELECT c.* FROM collected c "
                "LEFT JOIN review_queue q ON q.key = c.key "
                "WHERE q.key IS NULL AND c.reject = '' AND c.used_job = 0 "
                "  AND c.score >= ? "
                "ORDER BY c.score DESC, c.heat DESC LIMIT 1", (ms,)).fetchone()
        return store._row(r) if r else None
    except Exception as e:
        log.debug("挑待审失败: %s", e)
        return None


def push_mark(key: str, chat_id: int = 0, message_id: int = 0) -> None:
    """记下「这条已经推给管理员了」。"""
    try:
        init_db()
        with queue.db() as con:
            con.execute(
                "INSERT INTO review_queue (key,state,pushed_at,chat_id,message_id) "
                "VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                "pushed_at=excluded.pushed_at, chat_id=excluded.chat_id, "
                "message_id=excluded.message_id",
                (key, "pending", queue.now_iso(), int(chat_id or 0), int(message_id or 0)))
    except Exception as e:
        log.debug("标记待审失败: %s", e)


def get_state(key: str) -> dict | None:
    try:
        init_db()
        with queue.db() as con:
            r = con.execute("SELECT * FROM review_queue WHERE key=?", (key,)).fetchone()
        return dict(r) if r else None
    except Exception:
        return None


def decide(key: str, *, approved: bool, job_id: int = 0) -> tuple[bool, str]:
    """管理员点了按钮。通过才发（发由调用方做），这里只记状态。"""
    st = get_state(key)
    if not st:
        return False, "这条不在待审队列里"
    if st.get("state") != "pending":
        return False, "这条已经处理过了"
    try:
        init_db()
        with queue.db() as con:
            con.execute(
                "UPDATE review_queue SET state=?, decided_at=?, job_id=? WHERE key=?",
                ("approved" if approved else "skipped", queue.now_iso(),
                 int(job_id or 0), key))
        return True, ("已通过" if approved else "已跳过")
    except Exception as e:
        return False, f"记录失败：{type(e).__name__}"


def last_pushed_at() -> float:
    """上次推送时间（unix）。用来判断「一小时到了没」。"""
    try:
        init_db()
        with queue.db() as con:
            r = con.execute(
                "SELECT MAX(pushed_at) t FROM review_queue WHERE pushed_at <> ''").fetchone()
        if r and r["t"]:
            from datetime import datetime, timezone
            return datetime.fromisoformat(r["t"]).replace(
                tzinfo=timezone.utc).timestamp()
    except Exception:
        pass
    return 0.0


def due(now: float | None = None) -> bool:
    """该推下一条了吗（按间隔）。"""
    if get_conf("enabled", "0") != "1":
        return False
    try:
        mins = max(1, int(get_conf("interval_minutes", "60")))
    except Exception:
        mins = 60
    return (now or time.time()) - last_pushed_at() >= mins * 60


def stats() -> dict:
    try:
        init_db()
        with queue.db() as con:
            rows = con.execute("SELECT state, COUNT(*) c FROM review_queue "
                               "GROUP BY state").fetchall()
        by = {r["state"]: r["c"] for r in rows}
    except Exception:
        by = {}
    return {"by_state": by, "config": conf_all(),
            "last_pushed_at": last_pushed_at(),
            "candidate_score": (next_candidate() or {}).get("score", 0)}
