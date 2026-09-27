"""数据日报（core/analytics.py）的离线测试。

全部离线：只测解析 / 聚合 / 落库 / 重放 URL 构造 / 失败兜底。
真实联网采集（collect）在这里只验证「没有登录态时不炸、返回 ok=False」。
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from core import analytics, queue


@pytest.fixture(autouse=True)
def _fresh_schema():
    """每个用例先把 jobs / 分析表建好（conftest 已把 DATA_DIR 指到临时目录）。"""
    queue.init_db()
    analytics.init_db()
    _wipe()
    yield


def _wipe():
    """测试之间共用一个 SQLite 文件，必须清干净否则数据会串台。"""
    with queue.db() as con:
        for t in ("x_daily_metrics", "x_tweets", "x_tweet_activity",
                  "x_config", "jobs"):
            try:
                con.execute(f"DELETE FROM {t}")
            except Exception:
                pass


# ── 测试用假响应（结构照抄线上真实响应）──────────────────────

TS_2026_09_26 = 1790380800000

DAILY_PAYLOAD = {
    "data": {"viewer_v2": {"user_results": {"result": {
        "legacy": {"screen_name": "luoaowoo"},
        "current_time_series": [
            {"count": 606, "engagement_type": "Displayed",
             "is_engaging_user_verified": "true", "timestamp": TS_2026_09_26},
            {"count": 6494, "engagement_type": "Displayed",
             "is_engaging_user_verified": "false", "timestamp": TS_2026_09_26},
            {"count": 22, "engagement_type": "Fav",
             "is_engaging_user_verified": "true", "timestamp": TS_2026_09_26},
            {"count": 37, "engagement_type": "Fav",
             "is_engaging_user_verified": "false", "timestamp": TS_2026_09_26},
            {"count": 6, "engagement_type": "TweetCreate",
             "is_engaging_user_verified": "true", "timestamp": TS_2026_09_26},
        ],
    }}}}
}


def _tweet_result(tid="2103532100888756621", views="57", fav=1):
    return {
        "rest_id": tid,
        "views": {"count": views},
        "core": {"user_results": {"result": {"legacy": {"screen_name": "luoaowoo"}}}},
        "legacy": {
            "id_str": tid,
            "created_at": "Fri Sep 25 17:08:29 +0000 2026",
            "full_text": "🥳中秋快乐！大家",
            "favorite_count": fav, "retweet_count": 2, "reply_count": 3,
            "quote_count": 4, "bookmark_count": 5,
            "extended_entities": {"media": [{"id_str": "a"}, {"id_str": "b"}]},
        },
    }


def _timeline_payload(cursor="CUR1"):
    return {"data": {"user": {"result": {"timeline": {"timeline": {"instructions": [
        {"entry": {"content": {"itemContent": {"tweet_results": {
            "result": _tweet_result()}}}}},
        {"entry": {"content": {"cursor": {"cursorType": "Bottom", "value": cursor}}}},
    ]}}}}}}


ACTIVITY_PAYLOAD = {"data": {"tweet_result_by_rest_id": {"result": {
    "datapoints_grid": [
        {"metric_type": "Engagements", "metric_value": 1},
        {"metric_type": "DetailExpands"},
        {"metric_type": "Impressions", "metric_value": 56},
        {"metric_type": "LinkClicks"},
        {"metric_type": "ProfileVisits"},
        {"metric_type": "Follows", "metric_value": 2},
    ]}}}}


class _FakePage:
    """只记录 evaluate 的入参，返回预设响应。"""

    def __init__(self, response=None):
        self.calls = []
        self.response = response or {"status": 200, "body": "{}"}

    def evaluate(self, _js, arg):
        self.calls.append(arg)
        return self.response


# ── 解析 ───────────────────────────────────────────────────

def test_parse_daily_splits_verified_and_normal():
    rows = analytics.parse_daily(DAILY_PAYLOAD)
    assert len(rows) == 5
    disp = [r for r in rows if r["metric"] == "Displayed"]
    assert {r["verified"] for r in disp} == {True, False}
    assert {r["day"] for r in rows} == {"2026-09-26"}
    assert sum(r["count"] for r in disp) == 7100


def test_parse_daily_bad_payload_returns_empty():
    assert analytics.parse_daily({}) == []
    assert analytics.parse_daily(None) == []


def test_parse_tweets_extracts_metrics_and_day():
    rows = analytics.parse_tweets(_timeline_payload())
    assert len(rows) == 1
    t = rows[0]
    assert t["tweet_id"] == "2103532100888756621"
    assert t["day"] == "2026-09-25"
    assert t["created_at"] == "2026-09-25T17:08:29Z"
    assert t["views"] == 57 and t["likes"] == 1 and t["replies"] == 3
    assert t["retweets"] == 2 and t["quotes"] == 4 and t["bookmarks"] == 5
    assert t["media_count"] == 2
    assert t["kind"] == "orig"
    assert t["account"] == "luoaowoo"


def test_parse_tweets_detects_kind():
    for field, kind in (("in_reply_to_status_id_str", "reply"),
                        ("quoted_status_id_str", "quote")):
        node = _tweet_result()
        node["legacy"][field] = "123"
        payload = {"x": node}
        assert analytics.parse_tweets(payload)[0]["kind"] == kind
    node = _tweet_result()
    node["legacy"]["retweeted_status_result"] = {"result": {"rest_id": "9"}}
    assert analytics.parse_tweets({"x": node})[0]["kind"] == "retweet"


def test_parse_tweets_dedupes_same_id():
    node = _tweet_result()
    assert len(analytics.parse_tweets({"a": node, "b": node})) == 1


def test_parse_activity_maps_metrics():
    m = analytics.parse_activity(ACTIVITY_PAYLOAD, "123")
    assert m["tweet_id"] == "123"
    assert m["engagements"] == 1 and m["impressions"] == 56 and m["follows"] == 2
    # 没有 metric_value 的（0）也要落成 0，而不是缺键
    assert m["detail_expands"] == 0
    assert m["link_clicks"] == 0 and m["profile_visits"] == 0


def test_parse_activity_bad_payload():
    assert analytics.parse_activity({}, "1") == {}


# ── 落库 + 聚合 ────────────────────────────────────────────

def test_upsert_and_day_metrics_split():
    analytics.upsert_daily(analytics.parse_daily(DAILY_PAYLOAD))
    rows = analytics.day_metrics("2026-09-26")
    by_key = {r["key"]: r for r in rows}
    assert by_key["Displayed"]["verified"] == 606
    assert by_key["Displayed"]["normal"] == 6494
    assert by_key["Displayed"]["total"] == 7100
    assert by_key["Fav"]["verified"] == 22 and by_key["Fav"]["normal"] == 37
    assert by_key["Displayed"]["label"] == "浏览量"


def test_upsert_daily_is_idempotent_and_overwrites():
    payload = analytics.parse_daily(DAILY_PAYLOAD)
    analytics.upsert_daily(payload)
    analytics.upsert_daily(payload)
    assert len(analytics.day_metrics("2026-09-26")) == 3
    bumped = [dict(r) for r in payload if r["metric"] == "Displayed" and r["verified"]]
    bumped[0]["count"] = 999
    analytics.upsert_daily(bumped)
    got = {r["key"]: r for r in analytics.day_metrics("2026-09-26")}
    assert got["Displayed"]["verified"] == 999      # 覆盖而不是累加


def test_range_metrics_sums_across_days():
    analytics.upsert_daily([
        {"day": "2026-09-01", "metric": "Displayed", "verified": True, "count": 10},
        {"day": "2026-09-01", "metric": "Displayed", "verified": False, "count": 20},
        {"day": "2026-09-05", "metric": "Displayed", "verified": True, "count": 5},
        {"day": "2026-08-30", "metric": "Displayed", "verified": True, "count": 999},
    ])
    got = {r["key"]: r for r in analytics.range_metrics("2026-09-01", "2026-09-05")}
    assert got["Displayed"]["verified"] == 15
    assert got["Displayed"]["normal"] == 20
    assert got["Displayed"]["total"] == 35          # 区间外的不算


def test_tweets_query_and_detail():
    rows = analytics.parse_tweets(_timeline_payload())
    analytics.upsert_tweets(rows)
    got = analytics.tweets(day="2026-09-25")
    assert len(got) == 1
    tid = got[0]["tweet_id"]
    assert got[0]["url"].endswith(tid)
    one = analytics.tweet(tid)
    assert one["tweet_id"] == tid
    assert "activity" not in one                     # 还没采过单条分析
    analytics.upsert_activity([analytics.parse_activity(ACTIVITY_PAYLOAD, tid)])
    assert analytics.tweet(tid)["activity"]["impressions"] == 56
    assert analytics.tweet("nope") is None


# ── 机器人发的 vs 自己发的 ─────────────────────────────────

def test_link_bot_source_marks_by_tweet_id():
    tid_bot = "2103532100888756621"
    tid_man = "2083162479627243539"
    analytics.upsert_tweets([
        {"tweet_id": tid_bot, "day": "2026-09-25", "created_at": "2026-09-25T17:08:29Z"},
        {"tweet_id": tid_man, "day": "2026-09-25", "created_at": "2026-09-25T18:00:00Z"},
    ])
    jid = queue.enqueue(kind="text", tg_chat_id=1, tg_msg_id=2, raw_text="x",
                        status="pending")
    queue.mark(jid, "sent", tweet_id=tid_bot)

    analytics.link_bot_source()
    got = {t["tweet_id"]: t for t in analytics.tweets(day="2026-09-25")}
    assert got[tid_bot]["source"] == "bot"
    assert got[tid_bot]["job_id"] == jid
    assert got[tid_man]["source"] == "manual"
    assert got[tid_man]["job_id"] == 0


def test_daily_split_bot_vs_manual():
    analytics.upsert_tweets([
        {"tweet_id": "111", "day": "2026-09-20",
         "created_at": "2026-09-20T01:00:00Z", "views": 100, "likes": 5},
        {"tweet_id": "222", "day": "2026-09-20",
         "created_at": "2026-09-20T02:00:00Z", "views": 50, "likes": 1},
    ])
    jid = queue.enqueue(kind="text", tg_chat_id=1, tg_msg_id=99, raw_text="y",
                        status="pending")
    queue.mark(jid, "sent", tweet_id="111")
    analytics.link_bot_source()

    d = analytics.daily("2026-09-20")
    assert d["posts"] == 2
    assert d["split"]["bot"] == {"count": 1, "views": 100, "likes": 5}
    assert d["split"]["manual"] == {"count": 1, "views": 50, "likes": 1}
    assert d["post_views"] == 150
    assert d["is_today"] is False


# ── 重放 URL 构造 ──────────────────────────────────────────

def test_replay_rewrites_variables_in_url():
    page = _FakePage()
    url = ("https://x.com/i/api/graphql/ABC123/UserOriginalsTimeline"
           "?variables=%7B%22count%22%3A20%7D&features=%7B%7D")
    headers = {"authorization": "Bearer x", "cookie": "ct0=1", "host": "x.com"}
    analytics._replay(page, url, headers, {"count": 7, "cursor": "C"})

    new_url, sent_headers = page.calls[0]
    q = parse_qs(urlparse(new_url).query)
    assert json.loads(q["variables"][0]) == {"count": 7, "cursor": "C"}
    assert q["features"] == ["{}"]                   # 其它参数保持不动
    assert "cookie" not in sent_headers and "host" not in sent_headers
    assert sent_headers["authorization"] == "Bearer x"


def test_replay_accepts_raw_variables_string():
    page = _FakePage()
    analytics._replay(page, "https://x.com/i/api/graphql/A/B?v=1", {},
                      '{"tweetId":"999"}')
    q = parse_qs(urlparse(page.calls[0][0]).query)
    assert json.loads(q["variables"][0])["tweetId"] == "999"


# ── 时间边界 ───────────────────────────────────────────────

def test_utc_bounds_align_to_midnight():
    start, end = analytics._utc_bounds(92)
    assert start % analytics.DAY_MS == 0
    assert end % analytics.DAY_MS == 0
    assert (end - start) // analytics.DAY_MS == 92


def test_ms_to_iso_x_matches_x_format():
    assert analytics._ms_to_iso_x(TS_2026_09_26) == "2026-09-26T00:00:00.000Z"


# ── 配置（自己的表，不碰冻结的 settings.py）───────────────

def test_config_roundtrip():
    analytics.set_config({"push_enabled": "1", "push_time": "09:00"})
    assert analytics.get_config("push_enabled") == "1"
    assert analytics.get_config("push_time") == "09:00"
    assert analytics.get_config("missing", "dflt") == "dflt"


# ── 失败兜底：绝不抛异常 ───────────────────────────────────

def test_collect_returns_error_without_login_state():
    res = analytics.collect(days=7)
    assert res["ok"] is False
    assert isinstance(res["error"], str) and res["error"]


def test_refresh_never_raises_on_collect_failure(monkeypatch):
    def boom(**_kw):
        raise RuntimeError("网络炸了")
    monkeypatch.setattr(analytics, "collect", boom)
    res = analytics.refresh(days=7)
    assert res["ok"] is False and "网络炸了" in res["error"]


def test_refresh_saves_when_collect_succeeds(monkeypatch):
    monkeypatch.setattr(analytics, "collect", lambda **_kw: {
        "ok": True, "account": "luoaowoo", "pages": 3,
        "daily": analytics.parse_daily(DAILY_PAYLOAD),
        "tweets": analytics.parse_tweets(_timeline_payload()),
        "activity": [],
    })
    res = analytics.refresh(days=7)
    assert res["ok"] is True and res["account"] == "luoaowoo"
    assert res["saved"]["daily"] == 5 and res["saved"]["tweets"] == 1
    assert analytics.last_collected_at()
    assert analytics.get_config("last_account") == "luoaowoo"


def test_stats_reports_counts():
    analytics.upsert_daily(analytics.parse_daily(DAILY_PAYLOAD))
    analytics.upsert_tweets(analytics.parse_tweets(_timeline_payload()))
    st = analytics.stats()
    assert st["tweets"] >= 1 and st["days"] >= 1
    assert "ready" in st and "last_collected_at" in st


def test_overview_shape():
    analytics.upsert_daily(analytics.parse_daily(DAILY_PAYLOAD))
    o = analytics.overview(days=92)
    assert o["ok"] is True and o["days"] == 92
    assert len(o["from"]) == 10 and len(o["to"]) == 10
    assert isinstance(o["metrics"], list) and isinstance(o["tweets"], list)


def test_metric_labels_cover_known_keys():
    for key in ("Displayed", "Fav", "Reply", "Retweet", "TweetCreate"):
        assert key in analytics.METRIC_LABELS
