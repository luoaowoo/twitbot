"""采集结果入库 + 筛选 + 转发布（新文件）。

为什么单独建表：``core/queue.py`` 是冻结的，不能加字段/表。
这里在**同一个 SQLite 文件**里建自己的表，互不干扰。

流程：
    采集器 fetch() -> save_items() 入库
    -> list_items(min_likes=...) 按热度筛
    -> publish_item() 下载图片到 MEDIA_DIR + 建发布任务（走现有队列）
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from pathlib import Path

from .. import config, queue
from .base import Item, parse_count

log = logging.getLogger("twitbot.collect.store")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

SCHEMA = """
CREATE TABLE IF NOT EXISTS collected (
    key        TEXT PRIMARY KEY,
    platform   TEXT NOT NULL,
    item_id    TEXT NOT NULL,
    title      TEXT DEFAULT '',
    text       TEXT DEFAULT '',
    author     TEXT DEFAULT '',
    url        TEXT DEFAULT '',
    cover      TEXT DEFAULT '',
    images     TEXT DEFAULT '[]',
    likes      INTEGER DEFAULT 0,
    comments   INTEGER DEFAULT 0,
    collects   INTEGER DEFAULT 0,
    kind       TEXT DEFAULT 'image',
    heat       INTEGER DEFAULT 0,
    fetched_at TEXT NOT NULL,
    used_job   INTEGER DEFAULT 0,
    score      INTEGER DEFAULT 0,     -- 筛选打分（score.py 算的，越高越先推荐）
    reject     TEXT DEFAULT ''        -- 非空 = 被硬门槛拒了，不推荐
);
CREATE INDEX IF NOT EXISTS idx_collected_heat ON collected(heat DESC);
CREATE INDEX IF NOT EXISTS idx_collected_plat ON collected(platform, fetched_at DESC);
"""


def init_db() -> None:
    with queue.db() as con:
        con.executescript(SCHEMA)
        # 轻量迁移：老库补 score / reject 列
        cols = {r["name"] for r in con.execute("PRAGMA table_info(collected)")}
        if "score" not in cols:
            con.execute("ALTER TABLE collected ADD COLUMN score INTEGER DEFAULT 0")
        if "reject" not in cols:
            con.execute("ALTER TABLE collected ADD COLUMN reject TEXT DEFAULT ''")
        # 索引必须**在补列之后**建 —— 老库还没 score 列时，
        # 直接在 SCHEMA 里建索引会报 "no such column: score"（实测踩过）
        con.execute("CREATE INDEX IF NOT EXISTS idx_collected_score ON collected(score DESC)")


def save_items(items: list[Item]) -> int:
    """入库（同 key 覆盖，热度取新的）。返回写入条数。"""
    if not items:
        return 0
    init_db()
    ts = queue.now_iso()
    from . import score as _score
    rows = []
    for it in items:
        try:
            r = it.to_row()
            # 入库时就把「筛没筛得过 / 打几分」算好，列表和推荐直接读
            ev = _score.evaluate({**r, "images": it.images})
            rows.append((r["key"], r["platform"], r["item_id"], r["title"], r["text"],
                         r["author"], r["url"], r["cover"], r["images"],
                         int(r["likes"]), int(r["comments"]), int(r["collects"]),
                         r["kind"], it.heat(), r["fetched_at"] or ts, 0,
                         int(ev["score"]), ev["reject"]))
        except Exception:
            continue
    if not rows:
        return 0
    with queue.db() as con:
        con.executemany(
            "INSERT INTO collected (key,platform,item_id,title,text,author,url,cover,"
            "images,likes,comments,collects,kind,heat,fetched_at,used_job,"
            "score,reject) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "  title=excluded.title, text=excluded.text, author=excluded.author,"
            "  url=excluded.url, cover=excluded.cover, images=excluded.images,"
            "  likes=excluded.likes, comments=excluded.comments,"
            "  collects=excluded.collects, heat=excluded.heat,"
            "  fetched_at=excluded.fetched_at, score=excluded.score,"
            "  reject=excluded.reject",
            rows)
    return len(rows)


def _row(r) -> dict:
    try:
        imgs = json.loads(r["images"] or "[]")
    except Exception:
        imgs = []
    return {"key": r["key"], "platform": r["platform"], "item_id": r["item_id"],
            "title": r["title"] or "", "text": r["text"] or "",
            "author": r["author"] or "", "url": r["url"] or "",
            "cover": r["cover"] or "", "images": imgs,
            "likes": int(r["likes"] or 0), "comments": int(r["comments"] or 0),
            "collects": int(r["collects"] or 0), "kind": r["kind"] or "image",
            "heat": int(r["heat"] or 0), "fetched_at": r["fetched_at"] or "",
            "used_job": int(r["used_job"] or 0),
            "score": int((r["score"] if "score" in r.keys() else 0) or 0),
            "reject": (r["reject"] if "reject" in r.keys() else "") or ""}


def list_items(*, platform: str = "", min_likes: int = 0, min_comments: int = 0,
               unused_only: bool = False, limit: int = 100,
               min_score: int = 0, only_ok: bool = False,
               order: str = "heat") -> list[dict]:
    """列出采集结果。默认按热度；order='score' 时按筛选分（推荐用）。"""
    try:
        init_db()
        where, args = [], []
        if platform:
            where.append("platform = ?"); args.append(platform)
        if min_likes:
            where.append("likes >= ?"); args.append(int(min_likes))
        if min_comments:
            where.append("comments >= ?"); args.append(int(min_comments))
        if unused_only:
            where.append("used_job = 0")
        if min_score:
            where.append("score >= ?"); args.append(int(min_score))
        if only_ok:
            where.append("reject = ''")          # 被硬门槛拒的不进推荐池
        sql = "SELECT * FROM collected"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += (" ORDER BY score DESC, heat DESC LIMIT ?" if order == "score"
                else " ORDER BY heat DESC, likes DESC LIMIT ?")
        args.append(max(1, min(int(limit or 100), 1000)))
        with queue.db() as con:
            rows = con.execute(sql, tuple(args)).fetchall()
        return [_row(r) for r in rows]
    except Exception as e:
        log.debug("列采集结果失败：%s", e)
        return []


def get_item(key: str) -> dict | None:
    try:
        init_db()
        with queue.db() as con:
            r = con.execute("SELECT * FROM collected WHERE key=?", (key,)).fetchone()
        return _row(r) if r else None
    except Exception:
        return None


def stats() -> dict:
    try:
        init_db()
        with queue.db() as con:
            total = con.execute("SELECT COUNT(*) c FROM collected").fetchone()["c"]
            fresh = con.execute("SELECT COUNT(*) c FROM collected WHERE used_job=0").fetchone()["c"]
            by = con.execute("SELECT platform, COUNT(*) c FROM collected "
                             "GROUP BY platform").fetchall()
        return {"total": total, "unused": fresh,
                "by_platform": {r["platform"]: r["c"] for r in by}}
    except Exception:
        return {"total": 0, "unused": 0, "by_platform": {}}


def prune(keep: int = 2000) -> int:
    """只保留热度最高的 N 条（避免库无限涨）。"""
    try:
        init_db()
        with queue.db() as con:
            cur = con.execute(
                "DELETE FROM collected WHERE key NOT IN ("
                "  SELECT key FROM collected ORDER BY heat DESC LIMIT ?)", (int(keep),))
            return cur.rowcount or 0
    except Exception:
        return 0


# ══════════════════════════════════════════════════════════
# 转发布：下载图片 → 进现有发布队列
# ══════════════════════════════════════════════════════════

REFERER = {
    "xiaohongshu": "https://www.xiaohongshu.com/",
    "bilibili": "https://www.bilibili.com/",
    "weibo": "https://weibo.com/",
    "tieba": "https://tieba.baidu.com/",
}


def _fetch_bytes(url: str, platform: str = "", timeout: int = 30) -> bytes | None:
    # 各平台图床都校验 Referer —— 给错了就是 403（实测踩过：
    # 用小红的 Referer 去下 B站封面，直接失败）
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Referer": REFERER.get(platform or "", "https://www.bilibili.com/"),
            "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        if not raw or len(raw) < 1024:
            return None
        return raw
    except Exception as e:
        log.debug("取图失败 %s: %s", type(e).__name__, e)
        return None


def _download(url: str, dest: Path, platform: str = "", timeout: int = 30) -> bool:
    raw = _fetch_bytes(url, platform, timeout)
    if not raw:
        return False
    try:
        dest.write_bytes(raw)
        return True
    except Exception as e:
        log.debug("写文件失败 %s: %s", type(e).__name__, e)
        return False


# 图片 URL 里出现这些词基本就是 logo / 图标 / 头像，不是内容图
_BAD_IMG = ("logo", "avatar", "icon", "sprite", "default", "placeholder",
            "weibologo", "timeline_card_small", "thumb150", "square")


def _looks_like_junk(url: str) -> bool:
    low = (url or "").lower()
    return any(k in low for k in _BAD_IMG)


def pick_best_image(item: dict, *, max_try: int = 3) -> tuple[str, bytes] | None:
    """从候选图里挑**最竖**的那张，连字节一起返回。

    为什么要挑：参考号（@YongQuan 之类）发的全是竖图/方图 ——
    横图在 X 信息流里占屏小、吃亏。这里下载最多 3 张候选量宽高，
    优先竖图（高>=宽），其次挑比例最大的。
    """
    from . import imageinfo

    cands: list[str] = []
    cover = item.get("cover") or ""
    if cover:
        cands.append(cover)
    for u in (item.get("images") or []):
        if u and u not in cands:
            cands.append(u)
    # 明显是 logo/图标的先剔掉（参考号那种图上不该有站标）
    cands = [u for u in cands if not _looks_like_junk(u)] or cands

    best: tuple[float, bool, str, bytes] | None = None
    tried = 0
    for url in cands:
        if tried >= max(1, max_try):
            break
        tried += 1
        raw = _fetch_bytes(url, item.get("platform") or "")
        if not raw:
            continue
        wh = imageinfo.size_of(raw)
        score = imageinfo.ratio_score(wh)
        portrait = imageinfo.is_portrait(wh)
        if best is None or (portrait, score) > (best[1], best[0]):
            best = (score, portrait, url, raw)
        if portrait:
            break            # 已经拿到竖图，不用再试
    if best is None:
        return None
    return best[2], best[3]


def _ext_from(url: str, default: str = ".jpg") -> str:
    low = (url or "").lower().split("?")[0]
    for e in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        if low.endswith(e):
            return ".jpg" if e == ".jpeg" else e
    return default


def publish_item(key: str, *, text: str = "", with_source: bool = True,
                 immediately: bool = False, kind: str = "") -> tuple[bool, str, int]:
    """把一条采集结果发到 X（走现有队列）。

    返回 (成功?, 提示, job_id)。
    """
    it = get_item(key)
    if not it:
        return False, "采集记录不存在", 0
    if it.get("used_job"):
        return False, f"这条已经发过了（任务 #{it['used_job']}）", 0

    # 图片：从候选里挑**最竖**的一张（竖图在 X 信息流里占屏大；
    # 参考号发的全是竖图/方图）。没有图就拒绝 —— 我们只发图文。
    picked = pick_best_image(it)
    if picked is None:
        # 挑不出来（图挂了/格式认不出）时退回原来那张，别把能发的也卡死
        src = (it["images"] or [None])[0] or it["cover"]
        if not src:
            return False, "这条没有图片，发不了", 0
        raw = None
    else:
        src, raw = picked

    media_dir = Path(config.MEDIA_DIR)
    try:
        media_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return False, f"媒体目录不可用：{type(e).__name__}", 0

    name = f"{it['platform']}-{it['item_id']}{_ext_from(src)}"
    name = "".join(ch for ch in name if ch.isalnum() or ch in "._-")[:120]
    dest = media_dir / name
    if raw is not None:
        try:
            dest.write_bytes(raw)
        except Exception as e:
            return False, f"写图片失败：{type(e).__name__}", 0
    elif not dest.is_file():
        if not _download(src, dest, platform=it["platform"]):
            return False, "图片下载失败", 0

    # 正文：用户给的 + 可选出处
    body = (text or "").strip()
    if not body:
        # 参考号（@YongQuan / @XUEQIUxka）的配文都是 4~22 字的一句话吐槽，
        # 原帖标题往往是长篇描述 —— 所以这里**只截开头**，不整段搬。
        raw = (it.get("title") or it.get("text") or "").strip()
        raw = raw.split("\n")[0]                     # 只取第一行
        for sep in ("。", "！", "!", "？", "?"):      # 到第一个句末就停
            p = raw.find(sep)
            if 0 < p <= 40:
                raw = raw[:p + 1]
                break
        body = raw[:40] if len(raw) <= 40 else raw[:38] + "…"
    if with_source and it.get("author"):
        tag = f"\n\nvia {it['author']}"
        if it.get("url"):
            tag += f" {it['url']}"
        if len(body) + len(tag) <= 270:
            body += tag

    try:
        jid = queue.enqueue(kind="image", tg_chat_id=0, tg_msg_id=0, raw_text=body,
                            media_path=name, status=("pending" if immediately else "awaiting"))
    except Exception as e:
        return False, f"入队失败：{type(e).__name__}: {e}", 0
    if not jid:
        return False, "入队失败（可能重复）", 0

    try:
        init_db()
        with queue.db() as con:
            con.execute("UPDATE collected SET used_job=? WHERE key=?", (jid, key))
    except Exception:
        pass
    return True, f"已进队列 #{jid}" + ("（会自动发出）" if immediately else "（等确认）"), jid
