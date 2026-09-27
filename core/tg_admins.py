"""管理员名册 + 申请审批（新文件）。

规则（2026-09-26 定的）：
  * 管理员是一个**列表**（名字 / @用户名 / user id / 加入时间 / 谁批的），
    不再是 settings 里一串逗号分隔的数字。
  * 任何人发 ``/sign`` 申请；机器人推给**所有者**审批，批准后才成为管理员。
  * 不是管理员 → 只能发 ``/sign``，发别的会被提示一句。
  * 同一个人**一个月只能申请一次**。
  * 所有者默认 = 列表里第一个管理员，也可以在控制台改（``owner_id``）。

为什么不继续用 ``settings.tg_allowed_users``：
那个键是「一串数字」，没法表达名字、加入时间、审批人，也没法做待审批列表。
但为兼容老部署，第一次初始化时会把老值**迁移**进 tg_admins（见 ``ensure_seed``）。

硬性约束：所有对外函数**绝不抛异常**（机器人在消息热路径上调用）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import queue

log = logging.getLogger("twitbot.tgadmins")

UTC = timezone.utc
APPLY_COOLDOWN_DAYS = 30          # 一个月一次

SCHEMA = """
CREATE TABLE IF NOT EXISTS tg_admins (
    user_id  INTEGER PRIMARY KEY,
    username TEXT DEFAULT '',
    name     TEXT DEFAULT '',
    added_at TEXT NOT NULL,
    added_by INTEGER DEFAULT 0,
    source   TEXT DEFAULT 'sign',     -- owner | manual | sign
    seq      INTEGER DEFAULT 0        -- 加入顺序（同一秒加的人靠它分先后）
);
CREATE TABLE IF NOT EXISTS tg_signups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    username   TEXT DEFAULT '',
    name       TEXT DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
    created_at TEXT NOT NULL,
    decided_at TEXT DEFAULT '',
    decided_by INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_signups_user ON tg_signups(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_signups_status ON tg_signups(status, id);
"""


def init_db() -> None:
    with queue.db() as con:
        con.executescript(SCHEMA)
        # 轻量迁移：老库补 seq 列，并按 user_id 回填一个顺序
        cols = {r["name"] for r in con.execute("PRAGMA table_info(tg_admins)")}
        if "seq" not in cols:
            con.execute("ALTER TABLE tg_admins ADD COLUMN seq INTEGER DEFAULT 0")
            con.execute(
                "UPDATE tg_admins SET seq = (SELECT COUNT(*) FROM tg_admins b "
                " WHERE b.user_id <= tg_admins.user_id)")


def _now() -> str:
    return queue.now_iso()


def _parse(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except Exception:
        return None


def _person(row) -> dict:
    return {"user_id": int(row["user_id"]), "username": row["username"] or "",
            "name": row["name"] or "", "added_at": row["added_at"] or "",
            "added_by": int(row["added_by"] or 0), "source": row["source"] or "sign"}


# ══════════════════════════════════════════════════════════
# 所有者
# ══════════════════════════════════════════════════════════

def owner_id() -> int:
    """所有者 id。没显式设置就取**列表里第一个管理员**（最早的）。"""
    try:
        from . import analytics
        raw = (analytics.get_config("owner_id", "") or "").strip()
        if raw.lstrip("-").isdigit():
            return int(raw)
    except Exception:
        pass
    ensure_seed()          # 列表还空着的话，先把老白名单搬进来（第一个就是所有者）
    try:
        init_db()
        with queue.db() as con:
            row = con.execute(
                "SELECT user_id FROM tg_admins ORDER BY seq ASC, added_at ASC LIMIT 1"
            ).fetchone()
        return int(row["user_id"]) if row else 0
    except Exception:
        return 0


def set_owner(user_id) -> bool:
    """显式指定所有者（控制台用）。设成 0/空 = 回到「第一个管理员」。"""
    try:
        from . import analytics
        analytics.set_config({"owner_id": "" if not user_id else str(int(user_id))})
        return True
    except Exception as e:
        log.debug("设所有者失败：%s", e)
        return False


def is_owner(user_id) -> bool:
    try:
        oid = owner_id()
        return bool(oid) and int(user_id or 0) == oid
    except Exception:
        return False


# ══════════════════════════════════════════════════════════
# 管理员列表
# ══════════════════════════════════════════════════════════

def admins() -> list[dict]:
    """所有管理员。所有者排最前，其余按加入时间。"""
    try:
        init_db()
        with queue.db() as con:
            rows = con.execute(
                "SELECT * FROM tg_admins ORDER BY seq ASC, added_at ASC").fetchall()
    except Exception:
        return []
    out = [_person(r) for r in rows]
    oid = owner_id()
    out.sort(key=lambda p: (0 if p["user_id"] == oid else 1))
    for p in out:
        p["is_owner"] = (p["user_id"] == oid)
    return out


def admin_ids() -> set[int]:
    return {p["user_id"] for p in admins()}


def _count_admins() -> int:
    """纯 SQL 计数。**不要**走 admins() —— 那条路会回调 owner_id() → 递归。"""
    try:
        init_db()
        with queue.db() as con:
            return int(con.execute("SELECT COUNT(*) c FROM tg_admins").fetchone()["c"])
    except Exception:
        return 0


def is_admin(user_id) -> bool:
    try:
        uid = int(user_id or 0)
        if uid <= 0:
            return False
        init_db()
        with queue.db() as con:
            return con.execute("SELECT 1 FROM tg_admins WHERE user_id=?",
                               (uid,)).fetchone() is not None
    except Exception:
        return False


def get_admin(user_id) -> dict | None:
    try:
        uid = int(user_id or 0)
        init_db()
        with queue.db() as con:
            row = con.execute("SELECT * FROM tg_admins WHERE user_id=?",
                              (uid,)).fetchone()
        return _person(row) if row else None
    except Exception:
        return None


def add_admin(user_id, username: str = "", name: str = "",
              added_by: int = 0, source: str = "manual") -> bool:
    """加管理员（幂等）。已经有就顺手更新名字/@用户名。"""
    try:
        uid = int(user_id or 0)
        if uid <= 0:
            return False
        init_db()
        with queue.db() as con:
            nxt = con.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 s FROM tg_admins").fetchone()["s"]
            con.execute(
                "INSERT INTO tg_admins (user_id,username,name,added_at,added_by,source,seq) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                "  username=excluded.username, name=excluded.name",
                (uid, (username or "").strip(), (name or "").strip()[:120],
                 _now(), int(added_by or 0), (source or "manual")[:16], int(nxt or 1)))
        return True
    except Exception as e:
        log.debug("加管理员失败：%s", e)
        return False


def remove_admin(user_id) -> bool:
    """移除管理员。**不允许把最后一个管理员删掉**（否则没人能再批人）。"""
    try:
        uid = int(user_id or 0)
        if uid <= 0:
            return False
        init_db()
        with queue.db() as con:
            n = con.execute("SELECT COUNT(*) c FROM tg_admins").fetchone()["c"]
            if n <= 1 and con.execute("SELECT 1 FROM tg_admins WHERE user_id=?",
                                      (uid,)).fetchone():
                return False
            con.execute("DELETE FROM tg_admins WHERE user_id=?", (uid,))
        return True
    except Exception as e:
        log.debug("移除管理员失败：%s", e)
        return False


def ensure_seed() -> int:
    """老部署迁移：把 settings.tg_allowed_users 里那串数字搬进列表。

    只在列表**为空**时做一次（第一个搬进来的就是默认所有者）。
    返回搬进来的人数。
    """
    try:
        if _count_admins() > 0:        # 用纯计数，避免 admins() → owner_id() 递归
            return 0
        from . import settings as _settings
        raw = (_settings.get("tg_allowed_users", "") or "").strip()
        ids = [int(x) for x in raw.replace("，", ",").split(",")
               if x.strip().lstrip("-").isdigit()]
    except Exception:
        return 0
    n = 0
    for uid in ids:
        if add_admin(uid, source="owner" if not n else "manual"):
            n += 1
    if n:
        log.info("已把 %d 个旧白名单账号迁移成管理员", n)
    return n


# ══════════════════════════════════════════════════════════
# 申请 / 审批
# ══════════════════════════════════════════════════════════

def _signup(row) -> dict:
    return {"id": int(row["id"]), "user_id": int(row["user_id"]),
            "username": row["username"] or "", "name": row["name"] or "",
            "status": row["status"] or "pending",
            "created_at": row["created_at"] or "",
            "decided_at": row["decided_at"] or "",
            "decided_by": int(row["decided_by"] or 0)}


def last_signup(user_id) -> dict | None:
    try:
        init_db()
        with queue.db() as con:
            row = con.execute(
                "SELECT * FROM tg_signups WHERE user_id=? ORDER BY id DESC LIMIT 1",
                (int(user_id or 0),)).fetchone()
        return _signup(row) if row else None
    except Exception:
        return None


def can_apply(user_id) -> tuple[bool, str, int]:
    """能不能申请。返回 (可以?, 给用户看的原因, 还要等几天)。"""
    try:
        uid = int(user_id or 0)
    except Exception:
        return False, "无效的用户", 0
    if uid <= 0:
        return False, "无效的用户", 0
    if is_admin(uid):
        return False, "你已经是管理员了 ✅", 0
    last = last_signup(uid)
    if not last:
        return True, "", 0
    if last["status"] == "pending":
        return False, "你之前已经提交过申请，正在等审批 ⏳", 0
    created = _parse(last["created_at"])
    if created is None:
        return True, "", 0
    wait_until = created + timedelta(days=APPLY_COOLDOWN_DAYS)
    now = datetime.now(UTC)
    if now < wait_until:
        left = max(1, (wait_until - now).days + (1 if (wait_until - now).seconds else 0))
        return False, (f"一个月只能申请一次 —— 你上次是 "
                       f"{created.astimezone(UTC).strftime('%Y-%m-%d')} 申请的，"
                       f"还要等 {left} 天"), left
    return True, "", 0


def create_signup(user_id, username: str = "", name: str = "") -> tuple[bool, str, dict | None]:
    """提交申请。返回 (成功?, 提示语, 申请行)。"""
    ok, why, _ = can_apply(user_id)
    if not ok:
        return False, why, None
    try:
        init_db()
        with queue.db() as con:
            cur = con.execute(
                "INSERT INTO tg_signups (user_id,username,name,status,created_at) "
                "VALUES (?,?,?,'pending',?)",
                (int(user_id), (username or "").strip(), (name or "").strip()[:120], _now()))
            sid = cur.lastrowid
            row = con.execute("SELECT * FROM tg_signups WHERE id=?", (sid,)).fetchone()
        return True, "申请已提交", _signup(row)
    except Exception as e:
        log.debug("提交申请失败：%s", e)
        return False, "提交失败，请稍后再试", None


def pending_signups(limit: int = 50) -> list[dict]:
    try:
        init_db()
        with queue.db() as con:
            rows = con.execute(
                "SELECT * FROM tg_signups WHERE status='pending' ORDER BY id ASC LIMIT ?",
                (max(1, min(int(limit or 50), 200)),)).fetchall()
        return [_signup(r) for r in rows]
    except Exception:
        return []


def get_signup(signup_id) -> dict | None:
    try:
        init_db()
        with queue.db() as con:
            row = con.execute("SELECT * FROM tg_signups WHERE id=?",
                              (int(signup_id or 0),)).fetchone()
        return _signup(row) if row else None
    except Exception:
        return None


def decide(signup_id, approve: bool, decided_by: int = 0) -> tuple[bool, str, dict | None]:
    """审批。只有 pending 能被批（防手快点两下）。返回 (成功?, 提示语, 申请行)。"""
    row = get_signup(signup_id)
    if not row:
        return False, "找不到这条申请", None
    if row["status"] != "pending":
        return False, "这条申请已经处理过了", row
    status = "approved" if approve else "rejected"
    try:
        init_db()
        with queue.db() as con:
            con.execute(
                "UPDATE tg_signups SET status=?, decided_at=?, decided_by=? "
                "WHERE id=? AND status='pending'",
                (status, _now(), int(decided_by or 0), row["id"]))
            if con.execute("SELECT changes() c").fetchone()["c"] == 0:
                return False, "这条申请已经处理过了", get_signup(signup_id)
        if approve:
            add_admin(row["user_id"], row["username"], row["name"],
                      added_by=decided_by, source="sign")
        return True, ("已批准" if approve else "已拒绝"), get_signup(signup_id)
    except Exception as e:
        log.debug("审批失败：%s", e)
        return False, "审批失败，请稍后再试", row


def stats() -> dict:
    try:
        init_db()
        with queue.db() as con:
            n = con.execute("SELECT COUNT(*) c FROM tg_admins").fetchone()["c"]
            p = con.execute(
                "SELECT COUNT(*) c FROM tg_signups WHERE status='pending'").fetchone()["c"]
    except Exception:
        n = p = 0
    return {"admins": n, "pending": p, "owner_id": owner_id()}


# ══════════════════════════════════════════════════════════
# 给申请人发通知（控制台审批那条路用；机器人自己有 PTB bot 对象）
# ══════════════════════════════════════════════════════════

def _token() -> str:
    try:
        from . import settings as _s
        t = (_s.get("tg_token", "") or "").strip()
        if t:
            return t
    except Exception:
        pass
    try:
        from . import config as _c
        return (getattr(_c, "TG_TOKEN", "") or "").strip()
    except Exception:
        return ""


def notify_user(user_id, text: str) -> bool:
    """用 Bot API 给某个 user 发私信。**绝不抛异常，绝不泄露 token**。

    只有在对方跟机器人说过话（发过 /sign 就满足）时才能送达。
    """
    import json as _json
    import urllib.error
    import urllib.parse
    import urllib.request

    token = _token()
    try:
        uid = int(user_id or 0)
    except Exception:
        return False
    if not token or uid <= 0:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode(
        {"chat_id": uid, "text": str(text)[:4000],
         "disable_web_page_preview": "true"}).encode()
    try:
        with urllib.request.urlopen(url, data=body, timeout=10) as r:
            return bool(_json.loads(r.read().decode("utf-8", "replace")).get("ok"))
    except Exception as e:
        # 只记类型：异常文本里可能带 token
        log.warning("通知用户 %s 失败（%s）", uid, type(e).__name__)
        return False
