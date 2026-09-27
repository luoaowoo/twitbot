"""X 账号数据分析（新文件）—— 采集 + 存储 + 查询。

数据来源全部是 **X 网页端自己在用** 的 GraphQL 接口，用已保存的登录态取：

  1. ``accountOverviewDailyQuery`` —— 账号级 **按天** 序列，每一项都带
     ``is_engaging_user_verified``。这就是「浏览量 A+B（认证 A / 普通 B）」
     的数据源：浏览量/点赞/评论/发帖数/涨粉 都能拆成「认证用户 + 普通用户」。
  2. ``UserOriginalsTimeline`` —— 逐条推文，自带浏览量/点赞/转发/评论/引用/收藏，
     游标翻页可以一路翻到几个月前。
  3. ``TweetActivityQuery`` —— 单条推文的作者分析（曝光/参与/详情展开/资料访问/
     链接点击/涨粉）。**只有这一项是作者专属**，且 X **不提供单条的认证/普通拆分**。

采集手法（关键设计）：**先让 X 自己的前端发一次请求，我们截住 URL + 请求头，
改参数后重放**。好处是不硬编码 queryId / operation name ——
X 把 ``UserTweets`` 改名成 ``UserOriginalsTimeline`` 就是靠这个手法才不会烂。

实测记录（2026-09-26，服务器上真跑）：
  * 重放 92 天范围 → 200，668 个数据点、89 天
  * 游标翻页 → 每页 ~30 条，时间窗严格递减（``count`` 必须用 X 默认的 20，
    实测传 40 会乱）
  * 单条分析页 **没有** ``is_engaging_user_verified``（所以单条拆不了，只给总量）

硬性约束（对齐 AGENTS.md / CONTRACT_V2.md）：
  * ``collect()`` / ``refresh()`` **绝不抛异常穿透**，一律返回 ``{"ok": bool, ...}``。
  * 与发布共用同一把浏览器锁（``core.backends.browser._BROWSER_LOCK``），串行执行。
  * 不修改任何冻结文件；分析相关的配置写在自己的 ``x_config`` 表 ——
    ``core/settings.py`` 是白名单制（非 ``DEFAULTS`` 键静默丢弃）且已冻结。
  * 测试不得联网：除 ``collect()`` 外的读写都能在临时 DB 上跑。
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from . import queue

log = logging.getLogger("twitbot.analytics")

ANALYTICS_URL = "https://x.com/i/account_analytics"
PROFILE_URL = "https://x.com/{handle}"
STATUS_ANALYTICS_URL = "https://x.com/i/status/{tid}/analytics"

DAILY_OP = "accountOverviewDailyQuery"
TIMELINE_OP = "UserOriginalsTimeline"
ACTIVITY_OP = "TweetActivityQuery"

DAY_MS = 86_400_000
UTC = timezone.utc

# 指标中文名。Get 不到的键原样回显，方便 X 加新指标时不至于丢数据。
METRIC_LABELS = {
    "Displayed": "浏览量",
    "Fav": "点赞",
    "Reply": "评论",
    "Retweet": "转帖",
    "QuoteTweet": "引用",
    "ReplyCreate": "发起评论",
    "QuoteCreate": "发起引用",
    "Bookmark": "收藏",
    "Follow": "涨粉",
    "Unfollow": "掉粉",
    "ProfilePic": "头像点击",
    "HomeLinger": "主页停留",
    "TweetCreate": "发帖",
    "LingerTime": "停留时长",
}

# 日报卡片按这个顺序展示（只显示数据里真有的）
METRIC_ORDER = ("Displayed", "Fav", "Reply", "Retweet", "QuoteTweet",
                "Bookmark", "Follow", "ProfilePic", "TweetCreate")

# fetch 时不允许我们自己设置的请求头
_FORBIDDEN_HEADERS = {
    "host", "content-length", "cookie", "connection", "origin", "referer",
    "accept-encoding", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest",
}

_BROWSER_LOCK_WAIT = 900

SCHEMA = """
CREATE TABLE IF NOT EXISTS x_daily_metrics (
    day          TEXT    NOT NULL,
    metric       TEXT    NOT NULL,
    verified     INTEGER NOT NULL,
    count        INTEGER NOT NULL DEFAULT 0,
    collected_at TEXT    NOT NULL,
    PRIMARY KEY (day, metric, verified)
);
CREATE INDEX IF NOT EXISTS idx_xdaily_day ON x_daily_metrics(day);

CREATE TABLE IF NOT EXISTS x_tweets (
    tweet_id     TEXT PRIMARY KEY,
    account      TEXT    DEFAULT '',
    day          TEXT    DEFAULT '',
    created_at   TEXT    DEFAULT '',
    kind         TEXT    DEFAULT 'orig',
    text         TEXT    DEFAULT '',
    media_count  INTEGER DEFAULT 0,
    views        INTEGER DEFAULT 0,
    likes        INTEGER DEFAULT 0,
    replies      INTEGER DEFAULT 0,
    retweets     INTEGER DEFAULT 0,
    quotes       INTEGER DEFAULT 0,
    bookmarks    INTEGER DEFAULT 0,
    source       TEXT    DEFAULT 'manual',
    job_id       INTEGER DEFAULT 0,
    collected_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_xtweets_day ON x_tweets(day);

CREATE TABLE IF NOT EXISTS x_tweet_activity (
    tweet_id      TEXT PRIMARY KEY,
    impressions   INTEGER DEFAULT 0,
    engagements   INTEGER DEFAULT 0,
    detail_expands INTEGER DEFAULT 0,
    follows       INTEGER DEFAULT 0,
    link_clicks   INTEGER DEFAULT 0,
    profile_visits INTEGER DEFAULT 0,
    collected_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS x_config (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


# ══════════════════════════════════════════════════════════
# 基础设施
# ══════════════════════════════════════════════════════════

def init_db() -> None:
    """建分析相关的表。与 core.queue 共用同一个 SQLite 文件，互不干扰。"""
    with queue.db() as con:
        con.executescript(SCHEMA)


def _now() -> str:
    return queue.now_iso()


def get_config(key: str, default: str = "") -> str:
    try:
        init_db()
        with queue.db() as con:
            row = con.execute("SELECT value FROM x_config WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
    except Exception:
        return default


def set_config(items: dict[str, Any]) -> None:
    try:
        init_db()
        ts = _now()
        with queue.db() as con:
            for k, v in items.items():
                con.execute(
                    "INSERT INTO x_config (key,value,updated_at) VALUES (?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "updated_at=excluded.updated_at",
                    (str(k), str(v), ts))
    except Exception as e:
        log.debug("写 x_config 失败(忽略)：%s", e)


def last_collected_at() -> str:
    return get_config("last_collected_at", "")


def collect_age_seconds() -> float:
    """距上次采集过了多少秒；从没采集过返回一个很大的数。"""
    raw = last_collected_at()
    if not raw:
        return float("inf")
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return max(0.0, (datetime.now(UTC) - dt).total_seconds())
    except Exception:
        return float("inf")


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _int(v: Any) -> int:
    try:
        if v is None:
            return 0
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, str):
            v = v.strip().replace(",", "")
            if not v:
                return 0
        return int(float(v))
    except Exception:
        return 0


def _ms_to_iso(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ""


def _iso_to_iso(raw: str) -> str:
    """X 的 ``Fri Sep 25 17:08:29 +0000 2026`` → ``2026-09-25T17:08:29Z``。"""
    try:
        dt = datetime.strptime((raw or "").strip(), "%a %b %d %H:%M:%S %z %Y")
        return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ""


def _day_of(iso: str) -> str:
    return iso[:10] if iso else ""


# ══════════════════════════════════════════════════════════
# 写入
# ══════════════════════════════════════════════════════════

def upsert_daily(rows: list[dict]) -> int:
    if not rows:
        return 0
    init_db()
    ts = _now()
    with queue.db() as con:
        con.executemany(
            "INSERT INTO x_daily_metrics (day,metric,verified,count,collected_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(day,metric,verified) DO UPDATE SET "
            "count=excluded.count, collected_at=excluded.collected_at",
            [(r["day"], r["metric"], 1 if r["verified"] else 0,
              _int(r["count"]), ts) for r in rows])
    return len(rows)


def upsert_tweets(rows: list[dict]) -> int:
    if not rows:
        return 0
    init_db()
    ts = _now()
    with queue.db() as con:
        con.executemany(
            "INSERT INTO x_tweets (tweet_id,account,day,created_at,kind,text,"
            "media_count,views,likes,replies,retweets,quotes,bookmarks,"
            "source,job_id,collected_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(tweet_id) DO UPDATE SET account=excluded.account, "
            "day=excluded.day, created_at=excluded.created_at, kind=excluded.kind, "
            "text=excluded.text, media_count=excluded.media_count, "
            "views=excluded.views, likes=excluded.likes, replies=excluded.replies, "
            "retweets=excluded.retweets, quotes=excluded.quotes, "
            "bookmarks=excluded.bookmarks, collected_at=excluded.collected_at",
            [(str(r["tweet_id"]), r.get("account", ""), r.get("day", ""),
              r.get("created_at", ""), r.get("kind", "orig"), r.get("text", ""),
              _int(r.get("media_count")), _int(r.get("views")), _int(r.get("likes")),
              _int(r.get("replies")), _int(r.get("retweets")), _int(r.get("quotes")),
              _int(r.get("bookmarks")), r.get("source", "manual"),
              _int(r.get("job_id")), ts) for r in rows])
    return len(rows)


def upsert_activity(rows: list[dict]) -> int:
    rows = [r for r in rows if r.get("tweet_id")]
    if not rows:
        return 0
    init_db()
    ts = _now()
    with queue.db() as con:
        con.executemany(
            "INSERT INTO x_tweet_activity (tweet_id,impressions,engagements,"
            "detail_expands,follows,link_clicks,profile_visits,collected_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(tweet_id) DO UPDATE SET "
            "impressions=excluded.impressions, engagements=excluded.engagements, "
            "detail_expands=excluded.detail_expands, follows=excluded.follows, "
            "link_clicks=excluded.link_clicks, profile_visits=excluded.profile_visits, "
            "collected_at=excluded.collected_at",
            [(str(r["tweet_id"]), _int(r.get("impressions")), _int(r.get("engagements")),
              _int(r.get("detail_expands")), _int(r.get("follows")),
              _int(r.get("link_clicks")), _int(r.get("profile_visits")), ts)
             for r in rows])
    return len(rows)


def link_bot_source() -> int:
    """把推文标注成「机器人发的」还是「自己发的」。

    判据：``jobs.tweet_id`` 命中就是机器人发的（tweet_id 只有发布成功才回填，
    所以这是精确判定，不是猜的）。
    """
    try:
        init_db()
        with queue.db() as con:
            con.execute(
                "UPDATE x_tweets SET "
                "  source = CASE WHEN EXISTS ("
                "      SELECT 1 FROM jobs j WHERE j.tweet_id = x_tweets.tweet_id"
                "  ) THEN 'bot' ELSE 'manual' END, "
                "  job_id = COALESCE((SELECT j.id FROM jobs j "
                "      WHERE j.tweet_id = x_tweets.tweet_id LIMIT 1), 0)")
            return con.execute(
                "SELECT COUNT(*) c FROM x_tweets WHERE source='bot'").fetchone()["c"]
    except Exception as e:
        log.debug("标注来源失败(忽略)：%s", e)
        return 0


# ══════════════════════════════════════════════════════════
# 查询
# ══════════════════════════════════════════════════════════

def _metric_row(name: str, ver: int, nor: int) -> dict:
    return {"key": name, "label": METRIC_LABELS.get(name, name),
            "verified": ver, "normal": nor, "total": ver + nor}


def day_metrics(day: str) -> list[dict]:
    """某一天各指标的「认证 / 普通」拆分。"""
    try:
        init_db()
        with queue.db() as con:
            rows = con.execute(
                "SELECT metric, verified, count FROM x_daily_metrics WHERE day=?",
                (day,)).fetchall()
    except Exception:
        return []
    agg: dict[str, dict[str, int]] = {}
    for r in rows:
        slot = agg.setdefault(r["metric"], {"v": 0, "n": 0})
        if _int(r["verified"]):
            slot["v"] += _int(r["count"])
        else:
            slot["n"] += _int(r["count"])
    ordered = [m for m in METRIC_ORDER if m in agg]
    ordered += [m for m in sorted(agg) if m not in ordered]
    return [_metric_row(m, agg[m]["v"], agg[m]["n"]) for m in ordered]


def range_metrics(from_day: str, to_day: str) -> list[dict]:
    """日期区间（含首尾）各指标的「认证 / 普通」拆分。"""
    try:
        init_db()
        with queue.db() as con:
            rows = con.execute(
                "SELECT metric, verified, SUM(count) c FROM x_daily_metrics "
                "WHERE day >= ? AND day <= ? GROUP BY metric, verified",
                (from_day, to_day)).fetchall()
    except Exception:
        return []
    agg: dict[str, dict[str, int]] = {}
    for r in rows:
        slot = agg.setdefault(r["metric"], {"v": 0, "n": 0})
        if _int(r["verified"]):
            slot["v"] += _int(r["c"])
        else:
            slot["n"] += _int(r["c"])
    ordered = [m for m in METRIC_ORDER if m in agg]
    ordered += [m for m in sorted(agg) if m not in ordered]
    return [_metric_row(m, agg[m]["v"], agg[m]["n"]) for m in ordered]


def _tweet_view(row: Any) -> dict:
    return {
        "tweet_id": row["tweet_id"],
        "account": row["account"] or "",
        "day": row["day"] or "",
        "created_at": row["created_at"] or "",
        "kind": row["kind"] or "orig",
        "text": row["text"] or "",
        "media_count": _int(row["media_count"]),
        "views": _int(row["views"]),
        "likes": _int(row["likes"]),
        "replies": _int(row["replies"]),
        "retweets": _int(row["retweets"]),
        "quotes": _int(row["quotes"]),
        "bookmarks": _int(row["bookmarks"]),
        "source": row["source"] or "manual",
        "job_id": _int(row["job_id"]),
        "url": f"https://x.com/i/web/status/{row['tweet_id']}",
    }


def tweets(day: str | None = None, *, days: int | None = None,
           limit: int = 300) -> list[dict]:
    """按天或按最近 N 天取推文（新的在前）。"""
    try:
        init_db()
        with queue.db() as con:
            if day:
                rows = con.execute(
                    "SELECT * FROM x_tweets WHERE day=? ORDER BY created_at DESC LIMIT ?",
                    (day, max(1, limit))).fetchall()
            elif days is not None:
                cut = datetime.now(UTC).strftime("%Y-%m-%d")
                start = datetime.fromtimestamp(
                    time.time() - max(0, days - 1) * 86400, UTC).strftime("%Y-%m-%d")
                rows = con.execute(
                    "SELECT * FROM x_tweets WHERE day >= ? AND day <= ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (start, cut, max(1, limit))).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM x_tweets ORDER BY created_at DESC LIMIT ?",
                    (max(1, limit),)).fetchall()
    except Exception:
        return []
    return [_tweet_view(r) for r in rows]


def tweet(tweet_id: str) -> dict | None:
    try:
        init_db()
        with queue.db() as con:
            row = con.execute("SELECT * FROM x_tweets WHERE tweet_id=?",
                              (str(tweet_id),)).fetchone()
            if row is None:
                return None
            act = con.execute("SELECT * FROM x_tweet_activity WHERE tweet_id=?",
                              (str(tweet_id),)).fetchone()
    except Exception:
        return None
    out = _tweet_view(row)
    if act is not None:
        out["activity"] = {
            "impressions": _int(act["impressions"]),
            "engagements": _int(act["engagements"]),
            "detail_expands": _int(act["detail_expands"]),
            "follows": _int(act["follows"]),
            "link_clicks": _int(act["link_clicks"]),
            "profile_visits": _int(act["profile_visits"]),
        }
    return out


def _post_split(rows: list[dict]) -> dict:
    bot = {"count": 0, "views": 0, "likes": 0}
    man = {"count": 0, "views": 0, "likes": 0}
    for t in rows:
        slot = bot if t["source"] == "bot" else man
        slot["count"] += 1
        slot["views"] += t["views"]
        slot["likes"] += t["likes"]
    return {"bot": bot, "manual": man,
            "views": bot["views"] + man["views"],
            "likes": bot["likes"] + man["likes"],
            "count": bot["count"] + man["count"]}


def daily(day: str | None = None) -> dict:
    """单日汇总：账号级指标（拆认证/普通）+ 当天发的帖子的汇总。"""
    day = day or _today()
    metrics = day_metrics(day)
    tw = tweets(day)
    split = _post_split(tw)
    return {
        "ok": True,
        "day": day,
        "is_today": day == _today(),
        "metrics": metrics,
        "posts": split["count"],
        "post_views": split["views"],
        "split": split,
        "tweets": tw,
        "last_collected_at": last_collected_at(),
    }


def latest_day() -> str:
    """账号级数据里**最新的一天**。

    X 只发布「完整一天」的账号级数据 —— 实测 2026-09-26 当天拉取，
    序列最新只到 09-25。所以「今日新增浏览」只能是「最近完整日」。
    """
    try:
        init_db()
        with queue.db() as con:
            row = con.execute("SELECT MAX(day) d FROM x_daily_metrics").fetchone()
        return (row["d"] or "") if row else ""
    except Exception:
        return ""


def summary(days: int = 92) -> dict:
    """日报主入口：今日实时 + 最新完整日（账号级，含认证/普通）+ 近 N 天。

    两个「今日」口径都放进来（用户要求都要）：
      * ``today``  —— 今天**发的帖子**的累计浏览/点赞，来自逐条推文，实时
      * ``account_day`` —— 账号级**按天**数据（今天新增浏览量那种），
        但 X 只给完整天，所以这里是最新完整日的日期
    """
    today = _today()
    tw_today = tweets(today)
    split_today = _post_split(tw_today)
    acc_day = latest_day()
    acc_metrics = day_metrics(acc_day) if acc_day else []
    rng = overview(days)
    return {
        "ok": True,
        "today": today,
        "today_posts": split_today["count"],
        "today_views": split_today["views"],
        "today_likes": split_today["likes"],
        "today_split": split_today,
        "today_tweets": tw_today,
        "account_day": acc_day,
        "account_is_today": bool(acc_day and acc_day == today),
        "account_metrics": acc_metrics,
        "range_days": rng["days"],
        "range_from": rng["from"],
        "range_to": rng["to"],
        "range_metrics": rng["metrics"],
        "range_posts": rng["posts"],
        "range_views": rng["views"] if "views" in rng else rng["post_views"],
        "range_split": rng["split"],
        "last_collected_at": last_collected_at(),
    }


def overview(days: int = 92) -> dict:
    """近 N 天汇总（默认近三月）。"""
    days = max(1, min(int(days or 92), 400))
    end = datetime.now(UTC)
    start = datetime.fromtimestamp(end.timestamp() - (days - 1) * 86400, UTC)
    from_day, to_day = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    metrics = range_metrics(from_day, to_day)
    tw = tweets(days=days, limit=1000)
    split = _post_split(tw)
    return {
        "ok": True,
        "days": days,
        "from": from_day,
        "to": to_day,
        "metrics": metrics,
        "posts": split["count"],
        "post_views": split["views"],
        "split": split,
        "tweets": tw,
        "last_collected_at": last_collected_at(),
    }


def stats() -> dict:
    """采集状态快照（给控制台/机器人显示用）。"""
    try:
        init_db()
        with queue.db() as con:
            t = con.execute("SELECT COUNT(*) c FROM x_tweets").fetchone()["c"]
            d = con.execute("SELECT COUNT(DISTINCT day) c FROM x_daily_metrics").fetchone()["c"]
            b = con.execute(
                "SELECT COUNT(*) c FROM x_tweets WHERE source='bot'").fetchone()["c"]
    except Exception:
        t = d = b = 0
    age = collect_age_seconds()
    return {
        "ready": t > 0 or d > 0,
        "tweets": t,
        "days": d,
        "bot_tweets": b,
        "last_collected_at": last_collected_at(),
        "age_seconds": None if age == float("inf") else int(age),
        "account": get_config("last_account", ""),
    }


# ══════════════════════════════════════════════════════════
# 采集：截住 X 自己发的请求 → 改参数重放
# ══════════════════════════════════════════════════════════

_REPLAY_JS = """
async ([u, h]) => {
  try {
    const r = await fetch(u, {headers: h, credentials: "include"});
    const t = await r.text();
    return {status: r.status, body: t};
  } catch (e) {
    return {status: 0, body: "", error: String(e)};
  }
}
"""

# 读左下角「账号切换」按钮里的 @handle（比扫全页稳）
_HANDLE_JS = """
() => {
  const el = document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]');
  return el ? (el.innerText || "") : "";
}
"""


def _emitter(on_event: Callable[[str], None] | None):
    def emit(msg: str) -> None:
        try:
            if on_event:
                on_event(str(msg))
        except Exception:
            pass
        log.info("%s", msg)
    return emit


def _browser_lock():
    """借用发布用的那把浏览器锁，保证采集与发帖串行。"""
    try:
        from core.backends import browser as _b
        return getattr(_b, "_BROWSER_LOCK", None)
    except Exception:
        return None


def _clean_headers(h: Any) -> dict:
    out = {}
    for k, v in (h or {}).items():
        if k.lower() in _FORBIDDEN_HEADERS:
            continue
        out[k] = v
    return out


def _replay(page, url: str, headers: Any, variables: Any) -> dict:
    """照着抓到的 URL/请求头重放一次，只换 variables。返回 {status, body}。"""
    raw = (variables if isinstance(variables, str)
           else json.dumps(variables, separators=(",", ":"), ensure_ascii=False))
    pr = urlparse(url)
    q = parse_qs(pr.query)
    q["variables"] = [raw]
    new_url = urlunparse(pr._replace(query=urlencode(q, doseq=True)))
    try:
        res = page.evaluate(_REPLAY_JS, [new_url, _clean_headers(headers)])
    except Exception as e:
        return {"status": 0, "body": "", "error": f"{type(e).__name__}: {e}"}
    return res or {}


def _capture_into(cap: dict):
    def on_request(req):
        try:
            for op in (DAILY_OP, TIMELINE_OP, ACTIVITY_OP):
                if op in req.url and op not in cap:
                    cap[op] = {"url": req.url, "headers": req.headers}
        except Exception:
            pass
    return on_request


def _wait_for(page, cap: dict, op: str, seconds: float) -> bool:
    deadline = time.time() + max(0.0, seconds)
    while time.time() < deadline:
        if op in cap:
            return True
        try:
            page.wait_for_timeout(400)
        except Exception:
            break
    return op in cap


def _account_handle(page) -> str:
    """尽力拿到当前登录账号的 handle（不带 @）。拿不到返回空串。"""
    try:
        from core.backends.browser import BrowserBackend
        m = re.search(r"@([A-Za-z0-9_]+)", BrowserBackend.saved_account() or "")
        if m:
            return m.group(1)
    except Exception:
        pass
    try:
        txt = page.evaluate(_HANDLE_JS) or ""
        m = re.search(r"@([A-Za-z0-9_]+)", txt)
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


# ── 解析 ─────────────────────────────────────────────────

def _walk_tweets(node: Any, out: list, depth: int = 0) -> None:
    if depth > 18 or node is None:
        return
    if isinstance(node, dict):
        lg = node.get("legacy")
        if isinstance(lg, dict) and lg.get("id_str") and "full_text" in lg:
            out.append((node, lg))
        for v in node.values():
            _walk_tweets(v, out, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _walk_tweets(v, out, depth + 1)


def _walk_bottom_cursors(node: Any, out: list, depth: int = 0) -> None:
    if depth > 18 or node is None:
        return
    if isinstance(node, dict):
        if node.get("cursorType") == "Bottom" and node.get("value"):
            out.append(str(node["value"]))
        for v in node.values():
            _walk_bottom_cursors(v, out, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _walk_bottom_cursors(v, out, depth + 1)


def _media_count(lg: dict) -> int:
    for key in ("extended_entities", "entities"):
        media = ((lg.get(key) or {}).get("media")) or []
        if media:
            return len(media)
    return 0


def parse_tweets(payload: Any, *, account: str = "") -> list[dict]:
    """从 GraphQL 响应里解析出推文行（纯函数，便于离线测试）。"""
    pairs: list = []
    _walk_tweets(payload, pairs)
    rows, seen = [], set()
    for node, lg in pairs:
        tid = str(lg.get("id_str") or "")
        if not tid or tid in seen:
            continue
        seen.add(tid)
        created = _iso_to_iso(lg.get("created_at") or "")
        handle = account
        try:
            handle = (((node.get("core") or {}).get("user_results") or {})
                      .get("result") or {}).get("legacy", {}).get("screen_name") or account
        except Exception:
            pass
        if lg.get("retweeted_status_result"):
            kind = "retweet"
        elif lg.get("quoted_status_id_str"):
            kind = "quote"
        elif lg.get("in_reply_to_status_id_str"):
            kind = "reply"
        else:
            kind = "orig"
        rows.append({
            "tweet_id": tid,
            "account": handle or "",
            "day": _day_of(created),
            "created_at": created,
            "kind": kind,
            "text": (lg.get("full_text") or "").strip(),
            "media_count": _media_count(lg),
            "views": _int((node.get("views") or {}).get("count")),
            "likes": _int(lg.get("favorite_count")),
            "replies": _int(lg.get("reply_count")),
            "retweets": _int(lg.get("retweet_count")),
            "quotes": _int(lg.get("quote_count")),
            "bookmarks": _int(lg.get("bookmark_count")),
        })
    return rows


def parse_daily(payload: Any) -> list[dict]:
    """从 accountOverviewDailyQuery 响应解析出按天、按认证/普通的行。"""
    try:
        series = (payload["data"]["viewer_v2"]["user_results"]["result"]
                  ["current_time_series"])
    except Exception:
        return []
    rows = []
    for e in series or []:
        if not isinstance(e, dict):
            continue
        metric = str(e.get("engagement_type") or "").strip()
        day = _day_of(_ms_to_iso(e.get("timestamp")))
        if not metric or not day:
            continue
        verified = str(e.get("is_engaging_user_verified") or "").lower() == "true"
        rows.append({"day": day, "metric": metric, "verified": verified,
                     "count": _int(e.get("count"))})
    return rows


_ACTIVITY_KEYS = {
    "Impressions": "impressions",
    "Engagements": "engagements",
    "DetailExpands": "detail_expands",
    "Follows": "follows",
    "LinkClicks": "link_clicks",
    "ProfileVisits": "profile_visits",
}


def parse_activity(payload: Any, tweet_id: str) -> dict:
    """从 TweetActivityQuery 响应解析单条作者分析。"""
    try:
        grid = (payload["data"]["tweet_result_by_rest_id"]["result"]
                ["datapoints_grid"])
    except Exception:
        return {}
    out = {"tweet_id": str(tweet_id)}
    for d in grid or []:
        if not isinstance(d, dict):
            continue
        key = _ACTIVITY_KEYS.get(str(d.get("metric_type") or ""))
        if key:
            out[key] = _int(d.get("metric_value"))
    return out


def _viewer_handle(payload: Any) -> str:
    """账号分析响应里就带着当前登录账号的 screen_name。"""
    try:
        lg = (payload["data"]["viewer_v2"]["user_results"]["result"].get("legacy") or {})
        return str(lg.get("screen_name") or "")
    except Exception:
        return ""


def _ms_to_iso_x(ms: int) -> str:
    """X 自己用的毫秒时间戳文案：``2026-09-20T00:00:00.000Z``。"""
    return datetime.fromtimestamp(int(ms) / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _utc_bounds(days: int) -> tuple[int, int]:
    """返回 (start_ms, end_ms)，两端都对齐到 UTC 零点。"""
    now_ms = int(time.time() * 1000)
    end_ms = (now_ms // DAY_MS + 1) * DAY_MS
    return end_ms - max(1, days) * DAY_MS, end_ms


def _variables_of(cap_entry: dict) -> Any:
    try:
        return json.loads(parse_qs(urlparse(cap_entry["url"]).query)["variables"][0])
    except Exception:
        return None


def _collect_daily(page, cap_entry: dict, days: int) -> tuple[list[dict], str]:
    base = _variables_of(cap_entry)
    if not isinstance(base, dict):
        return [], ""
    start_ms, end_ms = _utc_bounds(days)
    span = max(1, end_ms - start_ms)
    v = dict(base)
    v.update({
        "current_from": start_ms, "current_to": end_ms,
        "current_from_iso": _ms_to_iso_x(start_ms),
        "current_to_iso": _ms_to_iso_x(end_ms),
        "prev_from": start_ms - span, "prev_to": start_ms,
        "prev_from_iso": _ms_to_iso_x(start_ms - span),
        "prev_to_iso": _ms_to_iso_x(start_ms),
    })
    res = _replay(page, cap_entry["url"], cap_entry["headers"], v)
    if _int(res.get("status")) != 200:
        log.warning("按天数据重放失败：status=%s", res.get("status"))
        return [], ""
    try:
        payload = json.loads(res.get("body") or "{}")
    except Exception:
        return [], ""
    return parse_daily(payload), _viewer_handle(payload)


def _collect_timeline(page, cap_entry: dict, days: int,
                      max_pages: int = 40) -> tuple[list[dict], int]:
    """游标翻页把近 N 天的推文全部翻出来（新的在前）。"""
    base = _variables_of(cap_entry)
    if not isinstance(base, dict):
        return [], 0
    cutoff_ms = (time.time() - max(1, days) * 86400) * 1000
    cursor: str | None = None
    collected: dict[str, dict] = {}
    pages = 0
    for _ in range(max(1, max_pages)):
        v = dict(base)
        v["cursor"] = cursor
        # X 默认就是 20；实测传 40 会让翻页乱序，必须保持 20
        v["count"] = 20
        res = _replay(page, cap_entry["url"], cap_entry["headers"], v)
        if _int(res.get("status")) != 200:
            log.warning("时间线重放失败：status=%s", res.get("status"))
            break
        try:
            payload = json.loads(res.get("body") or "{}")
        except Exception:
            break
        pages += 1
        page_rows = parse_tweets(payload)
        fresh = [r for r in page_rows if r["tweet_id"] not in collected]
        for r in page_rows:
            collected[r["tweet_id"]] = r
        if not fresh:
            break                      # 没有新东西 = 翻到头了
        cursors: list = []
        _walk_bottom_cursors(payload, cursors)
        next_cursor = cursors[-1] if cursors else None
        if not next_cursor or next_cursor == cursor:
            break
        # 这一页新增的推文全都早于 cutoff 就收工
        newest_stamps = [r["created_at"] for r in fresh if r["created_at"]]
        if newest_stamps:
            try:
                newest_ms = max(
                    datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
                    .replace(tzinfo=UTC).timestamp() * 1000 for s in newest_stamps)
                if newest_ms < cutoff_ms:
                    break
            except Exception:
                pass
        cursor = next_cursor
        time.sleep(0.8)                # 别把 X 惹毛
    rows = sorted(collected.values(), key=lambda r: r["created_at"], reverse=True)
    return rows, pages


def _collect_activity(page, cap_entry: dict, seed_id: str,
                      targets: list[dict]) -> list[dict]:
    """单条作者分析：拿一条抓到的请求当模板，文本替换 tweet id 后逐条重放。"""
    base = _variables_of(cap_entry)
    if base is None:
        return []
    raw = json.dumps(base, separators=(",", ":"), ensure_ascii=False)
    out = []
    for t in targets:
        tid = str(t["tweet_id"])
        res = _replay(page, cap_entry["url"], cap_entry["headers"],
                      raw.replace(seed_id, tid))
        if _int(res.get("status")) != 200:
            continue
        try:
            payload = json.loads(res.get("body") or "{}")
        except Exception:
            continue
        parsed = parse_activity(payload, tid)
        if len(parsed) > 1:
            out.append(parsed)
        time.sleep(0.4)
    return out


def collect(days: int = 92, *, on_event: Callable[[str], None] | None = None,
            max_pages: int = 40, activity_recent_days: int = 7,
            activity_max: int = 30) -> dict:
    """跑一次完整采集。**绝不抛异常**，失败返回 ``{"ok": False, "error": ...}``。"""
    started = time.time()
    emit = _emitter(on_event)
    days = max(1, min(int(days or 92), 400))

    try:
        from core.backends.browser import BrowserBackend
    except Exception as e:
        return {"ok": False, "error": f"浏览器后端不可用：{type(e).__name__}: {e}"}

    be = BrowserBackend()
    ok, why = be.available()
    if not ok:
        return {"ok": False, "error": f"浏览器后端不可用：{why}"}

    lock = _browser_lock()
    acquired = False
    if lock is not None:
        acquired = lock.acquire(timeout=_BROWSER_LOCK_WAIT)
        if not acquired:
            return {"ok": False, "error": "浏览器正忙（可能在发帖），稍后再试"}

    out: dict = {"ok": False, "daily": [], "tweets": [], "activity": [],
                 "account": "", "pages": 0, "error": ""}
    try:
        with be._open_session(headless=True) as s:
            page = s.context.new_page()
            cap: dict = {}
            page.on("request", _capture_into(cap))

            emit("打开 X 账号分析页…")
            page.goto(ANALYTICS_URL, wait_until="domcontentloaded")
            _wait_for(page, cap, DAILY_OP, 20)

            handle = ""
            if DAILY_OP in cap:
                emit(f"取按天数据（近 {days} 天，含认证/普通拆分）…")
                out["daily"], handle = _collect_daily(page, cap[DAILY_OP], days)
            if not handle:
                handle = _account_handle(page)
            out["account"] = handle

            if not handle:
                out["error"] = ("没识别出登录账号：请先在控制台「登录 X」，"
                                "或确认登录态没过期")
                return out

            emit(f"打开 @{handle} 的推文列表…")
            page.goto(PROFILE_URL.format(handle=handle), wait_until="domcontentloaded")
            _wait_for(page, cap, TIMELINE_OP, 20)
            if TIMELINE_OP in cap:
                emit("翻页收推文（最多 40 页）…")
                out["tweets"], out["pages"] = _collect_timeline(
                    page, cap[TIMELINE_OP], days, max_pages)

            if not out["daily"] and not out["tweets"]:
                out["error"] = ("没拿到任何数据：通常是登录态失效（X 跳到了登录页）。"
                                "请在控制台重新「登录 X」后重试")
                return out

            # 单条作者分析是加分项：失败就当没有，不影响主流程
            recent = [t for t in out["tweets"]
                      if t["created_at"] >=
                      datetime.fromtimestamp(
                          time.time() - activity_recent_days * 86400, UTC)
                      .strftime("%Y-%m-%dT%H:%M:%SZ")][:max(0, activity_max)]
            if recent:
                emit(f"补单条分析（最近 {len(recent)} 条）…")
                try:
                    page.goto(STATUS_ANALYTICS_URL.format(tid=recent[0]["tweet_id"]),
                              wait_until="domcontentloaded")
                    _wait_for(page, cap, ACTIVITY_OP, 20)
                    if ACTIVITY_OP in cap:
                        out["activity"] = _collect_activity(
                            page, cap[ACTIVITY_OP], str(recent[0]["tweet_id"]), recent)
                except Exception as e:
                    log.info("单条分析跳过：%s", e)

            out["ok"] = True
            return out
    except Exception as e:
        log.exception("采集异常")
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    finally:
        if acquired:
            try:
                lock.release()
            except Exception:
                pass
        log.info("采集结束：ok=%s 用时 %.1fs", out.get("ok"), time.time() - started)


def save(result: dict) -> dict:
    """把 collect() 的产物落库，并重新标注「机器人发的 / 自己发的」。"""
    init_db()
    n_daily = upsert_daily(result.get("daily") or [])
    n_tweets = upsert_tweets(result.get("tweets") or [])
    bot_n = link_bot_source()
    n_act = upsert_activity(result.get("activity") or [])
    set_config({"last_collected_at": _now(),
                "last_account": result.get("account") or ""})
    return {"daily": n_daily, "tweets": n_tweets,
            "activity": n_act, "bot_tweets": bot_n}


def refresh(days: int = 92, *, on_event: Callable[[str], None] | None = None,
            **kw) -> dict:
    """采集 + 落库，一步到位。**绝不抛异常**。"""
    try:
        res = collect(days=days, on_event=on_event, **kw)
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error") or "采集失败"}
        saved = save(res)
        return {"ok": True, "account": res.get("account") or "",
                "pages": _int(res.get("pages")), "saved": saved}
    except Exception as e:
        log.exception("refresh 异常")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
