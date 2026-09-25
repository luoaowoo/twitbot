"""SQLite 队列（冻结）—— 幂等入队、原子出队、状态机。

状态：pending（待发）| awaiting（等人确认）| sent | failed | canceled | duplicate
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_chat_id   INTEGER NOT NULL DEFAULT 0,
    tg_msg_id    INTEGER NOT NULL DEFAULT 0,
    kind         TEXT    NOT NULL,
    raw_text     TEXT    DEFAULT '',
    tweet_text   TEXT    DEFAULT '',
    media_path   TEXT    DEFAULT '',
    quote_id     TEXT    DEFAULT '',
    content_hash TEXT    DEFAULT '',
    backend      TEXT    DEFAULT '',
    status       TEXT    DEFAULT 'pending',
    attempts     INTEGER DEFAULT 0,
    tweet_id     TEXT    DEFAULT '',
    tweet_url    TEXT    DEFAULT '',
    error        TEXT    DEFAULT '',
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    UNIQUE (tg_chat_id, tg_msg_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, id);
CREATE INDEX IF NOT EXISTS idx_jobs_hash ON jobs(content_hash, status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
"""


def db(path: Path | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(path or config.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")     # 并发读写更稳（web + worker 同时跑）
    con.execute("PRAGMA busy_timeout=30000")
    return con


def init_db() -> None:
    with db() as con:
        con.executescript(SCHEMA)
        # 轻量迁移：老库补 backend 列
        cols = {r["name"] for r in con.execute("PRAGMA table_info(jobs)")}
        if "backend" not in cols:
            con.execute("ALTER TABLE jobs ADD COLUMN backend TEXT DEFAULT ''")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def enqueue(
    *, kind: str, tg_chat_id: int = 0, tg_msg_id: int = 0, raw_text: str = "",
    media_path: str = "", quote_id: str = "", content_hash: str = "",
    status: str = "pending", backend: str = "",
) -> int | None:
    """入队。重复的 (chat,msg) 返回 None（长轮询重投时靠这个防重复发推）。

    tg_chat_id/tg_msg_id 都为 0 时（Web 手工投料）不做唯一性约束，
    改用 content_hash + 时间窗去重（由调用方 is_dup_content 判断）。
    """
    ts = now_iso()
    fields = dict(
        tg_chat_id=tg_chat_id, tg_msg_id=tg_msg_id, kind=kind, raw_text=raw_text,
        media_path=media_path, quote_id=quote_id or "", content_hash=content_hash,
        status=status, backend=backend, created_at=ts, updated_at=ts,
    )
    # Web 投料没有 tg_msg_id：用负数时间戳占位，保证 UNIQUE 不误伤
    if tg_chat_id == 0 and tg_msg_id == 0:
        fields["tg_msg_id"] = -int(time.time() * 1000 % 2_000_000_000)
    cols, ph = ",".join(fields), ",".join("?" * len(fields))
    with db() as con:
        try:
            cur = con.execute(f"INSERT INTO jobs ({cols}) VALUES ({ph})", tuple(fields.values()))
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return None


def is_dup_content(h: str, window: int | None = None) -> bool:
    w = config.DEDUP_WINDOW if window is None else window
    if not h or w <= 0:
        return False
    cutoff = datetime.fromtimestamp(time.time() - w, timezone.utc).isoformat(timespec="seconds")
    with db() as con:
        row = con.execute(
            "SELECT 1 FROM jobs WHERE content_hash=? AND created_at>? "
            "AND status IN ('pending','awaiting','sent') LIMIT 1",
            (h, cutoff),
        ).fetchone()
    return row is not None


def claim_next() -> sqlite3.Row | None:
    """原子取一条 pending 并递增 attempts，返回**递增后**的最新行。

    必须同一事务内「选中->更新->重读」：返回旧行会让 worker 读到偏小的 attempts，
    反复 429 时把已达上限的任务标回 pending，而出队过滤 attempts<MAX 又不再取它，
    该任务永久卡死。BEGIN IMMEDIATE 兼防并发双取。
    """
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT id FROM jobs WHERE status='pending' AND attempts<? ORDER BY id LIMIT 1",
            (config.MAX_ATTEMPTS,),
        ).fetchone()
        if row is None:
            return None
        job_id = row["id"]
        con.execute(
            "UPDATE jobs SET attempts=attempts+1, updated_at=? WHERE id=? AND status='pending'",
            (now_iso(), job_id),
        )
        return con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def mark(job_id: int, status: str, **kw) -> None:
    sets, vals = ["status=?", "updated_at=?"], [status, now_iso()]
    for k, v in kw.items():
        sets.append(f"{k}=?")
        vals.append(v)
    vals.append(job_id)
    with db() as con:
        con.execute(f"UPDATE jobs SET {','.join(sets)} WHERE id=?", tuple(vals))


def get(job_id: int) -> sqlite3.Row | None:
    with db() as con:
        return con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def month_sent_count(backend: str | None = None) -> int:
    prefix = datetime.now(timezone.utc).strftime("%Y-%m")
    sql = "SELECT COUNT(*) c FROM jobs WHERE status='sent' AND updated_at LIKE ?"
    args: list = [prefix + "%"]
    if backend:
        sql += " AND backend=?"
        args.append(backend)
    with db() as con:
        return int(con.execute(sql, tuple(args)).fetchone()["c"])


def stats() -> dict:
    with db() as con:
        rows = con.execute("SELECT status, COUNT(*) c FROM jobs GROUP BY status").fetchall()
        by_status = {r["status"]: r["c"] for r in rows}
        total = con.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"]
    return {
        "total": total,
        "by_status": by_status,
        "month_sent": month_sent_count(),
        "limit": config.MONTHLY_LIMIT,
    }


def recent(limit: int = 50, status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM jobs"
    args: list = []
    if status:
        sql += " WHERE status=?"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with db() as con:
        return [dict(r) for r in con.execute(sql, tuple(args)).fetchall()]


def reset_failed() -> int:
    with db() as con:
        return con.execute(
            "UPDATE jobs SET status='pending', attempts=0, error='' WHERE status='failed'"
        ).rowcount


def requeue(job_id: int) -> bool:
    with db() as con:
        n = con.execute(
            "UPDATE jobs SET status='pending', attempts=0, error='' "
            "WHERE id=? AND status IN ('failed','canceled')",
            (job_id,),
        ).rowcount
    return n > 0


def cancel(job_id: int) -> bool:
    with db() as con:
        n = con.execute(
            "UPDATE jobs SET status='canceled' WHERE id=? AND status IN ('pending','awaiting')",
            (job_id,),
        ).rowcount
    return n > 0


def queue_depth() -> int:
    with db() as con:
        return int(con.execute(
            "SELECT COUNT(*) c FROM jobs WHERE status='pending'"
        ).fetchone()["c"])
