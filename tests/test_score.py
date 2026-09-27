"""筛选打分（core/collect/score.py）的离线测试。

打分依据来自参考号 @YongQuan / @XUEQIUxka 的实测：
竖图方图为主、配文 4~22 字、一条 1~2 张图、横图要少。
"""
from __future__ import annotations

import pytest

from core.collect import score


def _it(**kw):
    base = {"images": ["https://x/a.jpg"], "likes": 1000, "title": "短标题"}
    base.update(kw)
    return base


# ── 硬门槛 ─────────────────────────────────────────────────

def test_reject_no_image():
    assert "没有图片" in score.hard_reject({"images": [], "cover": "", "likes": 99})


def test_reject_logo_only():
    r = score.hard_reject({"images": ["https://h5.sinaimg.cn/weibologo.png"], "likes": 99})
    assert "站标" in r


def test_reject_zero_likes():
    assert score.hard_reject(_it(likes=0)) == "零赞"


def test_reject_wide_landscape():
    # 16:9 = 1.78 > 1.6 门槛
    assert "横图" in score.hard_reject(_it(wh=[1920, 1080]))


def test_keep_mild_landscape():
    """1.23 那条置顶必须能过（参考号就有横图）。"""
    assert score.hard_reject(_it(wh=[1080, 878])) == ""
    assert score.hard_reject(_it(wh=[1500, 1000])) == ""      # 3:2 也不砍


def test_reject_ad():
    assert score.hard_reject(_it(title="限时优惠券点击链接领取")) == "疑似广告"


# ── 打分 ───────────────────────────────────────────────────

def test_portrait_scores_higher_than_landscape():
    port = score.evaluate(_it(wh=[1080, 1440]))["score"]
    land = score.evaluate(_it(wh=[1080, 878]))["score"]
    assert port > land


def test_square_treated_as_portrait():
    assert score.evaluate(_it(wh=[1080, 1080]))["score"] >= score.evaluate(_it(wh=[1080, 1440]))["score"]


def test_comments_add_score():
    a = score.evaluate(_it(comments=0))["score"]
    b = score.evaluate(_it(comments=500))["score"]
    assert b > a


def test_heat_grows_with_likes_but_logarithmic():
    a = score.evaluate(_it(likes=10))["score"]
    b = score.evaluate(_it(likes=10000))["score"]
    assert b > a
    assert (b - a) < 90          # 取了对数，不该线性暴涨


def test_many_images_penalised():
    a = score.evaluate(_it(images=["https://x/%d.jpg" % i for i in range(2)]))["score"]
    b = score.evaluate(_it(images=["https://x/%d.jpg" % i for i in range(9)]))["score"]
    assert b < a


def test_long_title_penalised():
    a = score.evaluate(_it(title="短"))["score"]
    b = score.evaluate(_it(title="标题" * 60))["score"]
    assert b < a


def test_high_res_bonus():
    a = score.evaluate(_it(wh=[600, 800]))["score"]
    b = score.evaluate(_it(wh=[1080, 1440]))["score"]
    assert b > a


def test_evaluate_shape():
    r = score.evaluate(_it())
    assert set(r) == {"ok", "score", "reasons", "reject"}
    assert r["ok"] is True and r["reasons"]
    bad = score.evaluate({"images": [], "likes": 1})
    assert bad["ok"] is False and bad["score"] == -999


def test_junk_and_ad_helpers():
    assert score.looks_junk("https://x/avatar/1.png")
    assert not score.looks_junk("https://i2.hdslb.com/bfs/archive/a.jpg")
    assert score.looks_ad("加微信领取")
    assert not score.looks_ad("这个老板真有意思")

