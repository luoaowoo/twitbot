"""机器人「见过的人」名册（新文件）—— user id ↔ @用户名 的对应关系。

**为什么需要它**（2026-09-26 实测）：
Telegram Bot API 的 ``getChat`` 文档写的 chat_id 可以是 ``@username``，
但那个只对**公开频道 / 超级群**有效 —— 私聊用户压根查不到：

    getChat("@luoaowoo")  →  400 Bad Request: chat not found
    getChat("7584781186") →  200（数字 id 才行）

而机器人收到的每条消息里本来就带着 ``from_user.id`` / ``from_user.username``，
所以自己记一份名册，之后就能用 ``@用户名`` 反查 id ——
在控制台填管理员时不用再手抄一长串数字。

名册只记「谁跟机器人说过话」这种本来就对机器人可见的信息，
不记消息内容，也不进日志。
"""
from __future__ import annotations

import logging

from . import queue

log = logging.getLogger("twitbot.tgcontacts")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tg_contacts (
    user_id    INTEGER PRIMARY KEY,
    username   TEXT DEFAULT '',
    name       TEXT DEFAULT '',
    chat_id    INTEGER DEFAULT 0,
    chat_type  TEXT DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tg_contacts_uname ON tg_contacts(username);
"""


def init_db() -> None:
    with queue.db() as con:
        con.executescript(SCHEMA)


def _norm(query: str) -> str:
    return (query or "").strip().lstrip("@").lower()


def remember(user_id, username: str = "", name: str = "",
             chat_id: int = 0, chat_type: str = "") -> bool:
    """记下/更新一个人。**绝不抛异常**（调用方在消息热路径上）。"""
    try:
        uid = int(user_id or 0)
        if uid <= 0:
            return False
        init_db()
        ts = queue.now_iso()
        with queue.db() as con:
            con.execute(
                "INSERT INTO tg_contacts (user_id,username,name,chat_id,chat_type,"
                "first_seen,last_seen) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "  username=excluded.username, name=excluded.name, "
                "  chat_id=excluded.chat_id, chat_type=excluded.chat_type, "
                "  last_seen=excluded.last_seen",
                (uid, (username or "").strip(), (name or "").strip()[:120],
                 int(chat_id or 0), (chat_type or "").strip(), ts, ts))
        return True
    except Exception as e:
        log.debug("记名册失败(忽略)：%s", e)
        return False


def lookup(query: str) -> dict | None:
    """按 ``@用户名`` 或数字 id 查人。查不到返回 None。"""
    q = _norm(query)
    if not q:
        return None
    try:
        init_db()
        with queue.db() as con:
            if q.lstrip("-").isdigit():
                row = con.execute("SELECT * FROM tg_contacts WHERE user_id=?",
                                  (int(q),)).fetchone()
            else:
                row = con.execute(
                    "SELECT * FROM tg_contacts WHERE lower(username)=? "
                    "ORDER BY last_seen DESC LIMIT 1", (q,)).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    return {"id": int(row["user_id"]), "username": row["username"] or "",
            "name": row["name"] or "", "chat_id": int(row["chat_id"] or 0),
            "chat_type": row["chat_type"] or "",
            "last_seen": row["last_seen"] or ""}


def recent(limit: int = 20) -> list[dict]:
    """最近跟机器人说过话的人（新的在前）。"""
    try:
        init_db()
        with queue.db() as con:
            rows = con.execute(
                "SELECT * FROM tg_contacts ORDER BY last_seen DESC LIMIT ?",
                (max(1, min(int(limit or 20), 200)),)).fetchall()
    except Exception:
        return []
    return [{"id": int(r["user_id"]), "username": r["username"] or "",
             "name": r["name"] or "", "chat_id": int(r["chat_id"] or 0),
             "chat_type": r["chat_type"] or "",
             "last_seen": r["last_seen"] or ""} for r in rows]


def display(person: dict) -> str:
    """给人看的一行字：``洛嗷呜（@luoaowoo · 7988954881）``。"""
    name = (person or {}).get("name") or ""
    uname = (person or {}).get("username") or ""
    uid = (person or {}).get("id") or ""
    tail = " · ".join(x for x in ((("@" + uname) if uname else ""), str(uid)) if x)
    return f"{name}（{tail}）" if name else (tail or str(uid))

