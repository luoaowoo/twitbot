"""机器人「📊 数据日报」的离线测试（不联网）。"""
from __future__ import annotations

import asyncio

import pytest

import bot
from core import analytics, queue


@pytest.fixture(autouse=True)
def _seed():
    queue.init_db()
    analytics.init_db()
    with queue.db() as con:
        for _t in ("x_daily_metrics", "x_tweets", "x_tweet_activity",
                   "x_config", "jobs"):
            try:
                con.execute(f"DELETE FROM {_t}")
            except Exception:
                pass
    analytics.upsert_daily([
        {"day": "2026-09-25", "metric": "Displayed", "verified": True, "count": 606},
        {"day": "2026-09-25", "metric": "Displayed", "verified": False, "count": 6494},
        {"day": "2026-09-25", "metric": "Fav", "verified": True, "count": 22},
        {"day": "2026-09-25", "metric": "Fav", "verified": False, "count": 37},
    ])
    analytics.upsert_tweets([{
        "tweet_id": "2103532100888756621", "account": "luoaowoo",
        "day": analytics._today(), "created_at": analytics._today() + "T01:00:00Z",
        "kind": "orig", "text": "测试推文", "views": 57, "likes": 1,
        "replies": 2, "retweets": 3, "quotes": 4, "bookmarks": 5,
    }])
    analytics.link_bot_source()
    yield


class _Msg:
    """最小消息替身：记录 reply_text 的调用。"""

    def __init__(self):
        self.replies: list[tuple] = []

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw))

    @property
    def text(self):
        return self.replies[-1][0] if self.replies else ""


def _run(coro):
    return asyncio.run(coro)


# ── 文本命令 ───────────────────────────────────────────────

def test_command_daily_sends_summary():
    msg = _Msg()
    handled = _run(bot._maybe_an_command(msg, "日报"))
    assert handled is True
    body, kw = msg.replies[0]
    assert "数据日报" in body
    assert "今日实时" in body
    assert "近三月" in body
    assert "reply_markup" in kw          # 带按钮


def test_command_daily_accepts_leading_slash():
    msg = _Msg()
    assert _run(bot._maybe_an_command(msg, "/日报")) is True
    assert msg.replies


def test_command_three_months_lists_metrics():
    msg = _Msg()
    assert _run(bot._maybe_an_command(msg, "三月")) is True
    body = msg.replies[0][0]
    assert "近三月" in body
    assert "浏览量" in body
    assert "认证用户" in body and "普通用户" in body      # 带图例
    assert "✅" in body and "⚪" in body


def test_command_single_tweet_by_id():
    msg = _Msg()
    assert _run(bot._maybe_an_command(msg, "单条 2103532100888756621")) is True
    body = msg.replies[0][0]
    assert "2103532100888756621" in body
    assert "浏览量" in body and "点赞" in body


def test_command_single_tweet_by_url():
    msg = _Msg()
    assert _run(bot._maybe_an_command(
        msg, "单条 https://x.com/luoaowoo/status/2103532100888756621")) is True
    assert "2103532100888756621" in msg.replies[0][0]


def test_command_single_tweet_unknown_id():
    msg = _Msg()
    assert _run(bot._maybe_an_command(msg, "单条 999")) is True
    assert "没查到" in msg.replies[0][0]


def test_command_specific_day():
    msg = _Msg()
    assert _run(bot._maybe_an_command(msg, "日报 2026-09-25")) is True
    body = msg.replies[0][0]
    assert "2026-09-25" in body
    assert "7,100" in body            # 606 + 6494


def test_non_command_returns_false():
    msg = _Msg()
    assert _run(bot._maybe_an_command(msg, "今天天气不错")) is False
    assert not msg.replies


def test_random_number_is_not_a_command():
    msg = _Msg()
    assert _run(bot._maybe_an_command(msg, "1234567890")) is False


# ── 渲染 ───────────────────────────────────────────────────

def test_summary_text_shows_verified_and_normal_split():
    an = bot._an_module()
    text = bot._an_summary_text(an)
    assert "数据日报" in text
    assert "账号数据" in text
    assert "✅" in text and "⚪" in text


def test_tweet_text_marks_source_and_metrics():
    t = analytics.tweet("2103532100888756621")
    text = bot._an_tweet_text(t)
    assert "浏览量 57" in text
    assert "点赞 1" in text and "评论 2" in text
    assert "👤 自己发" in text or "🤖 机器人发" in text


def test_tweet_text_includes_activity_when_present():
    analytics.upsert_activity([{"tweet_id": "2103532100888756621",
                                "impressions": 56, "engagements": 1}])
    t = analytics.tweet("2103532100888756621")
    text = bot._an_tweet_text(t)
    assert "曝光 56" in text and "参与 1" in text


def test_tweet_list_buttons_carry_tweet_ids():
    rows = analytics.tweets(days=1, limit=10)
    kb = bot._an_tweet_kb(rows, rows[0]["day"], 0)
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert any(d.startswith("ant:") for d in datas)
    assert any(d == "anr" for d in datas)


def test_tweet_list_paginates():
    many = [{"tweet_id": f"10{i}", "day": "2026-09-25",
             "created_at": f"2026-09-25T{i:02d}:00:00Z", "views": i,
             "likes": 0, "replies": 0, "retweets": 0} for i in range(20)]
    analytics.upsert_tweets(many)
    rows = analytics.tweets(day="2026-09-25", limit=100)
    kb0 = bot._an_tweet_kb(rows, "2026-09-25", 0)
    d0 = [b.callback_data for row in kb0.inline_keyboard for b in row]
    assert any(d.startswith("anp:2026-09-25:1") for d in d0)     # 有下一页
    assert not any(d.startswith("anp:2026-09-25:-1") for d in d0)


def test_show_tweets_empty_day():
    msg = _Msg()
    _run(bot._an_show_tweets(msg, "1999-01-01"))
    assert "还没有推文记录" in msg.replies[0][0]


def test_show_tweets_renders_rows():
    day = analytics._today()
    msg = _Msg()
    _run(bot._an_show_tweets(msg, day))
    body = msg.replies[0][0]
    assert "的推文" in body
    assert "👁" in body and "❤" in body
    assert "测试推文" in body


def test_refresh_reports_failure(monkeypatch):
    monkeypatch.setattr(analytics, "refresh",
                        lambda *_a, **_k: {"ok": False, "error": "登录态失效"})
    msg = _Msg()

    async def go():
        await bot._an_refresh(msg)
        await asyncio.sleep(0.2)          # 等后台任务跑完

    _run(go())
    joined = "\n".join(t for t, _ in msg.replies)
    assert "开始采集" in joined
    assert "采集失败" in joined and "登录态失效" in joined


def test_refresh_success_then_summary(monkeypatch):
    monkeypatch.setattr(analytics, "refresh",
                        lambda *_a, **_k: {"ok": True, "saved": {}})
    msg = _Msg()

    async def go():
        await bot._an_refresh(msg)
        await asyncio.sleep(0.2)

    _run(go())
    joined = "\n".join(t for t, _ in msg.replies)
    assert "采集完成" in joined
    assert "数据日报" in joined


def test_menu_has_analytics_button():
    kb = None
    texts = []

    class _T:
        async def reply_text(self, text, **kw):
            texts.append(text)
            nonlocal kb
            kb = kw.get("reply_markup")

    _run(bot._show_menu(_T()))
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "an" in datas
    assert "数据日报" in texts[0]
