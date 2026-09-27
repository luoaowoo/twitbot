"""采集功能（core/collect/*）的离线测试。

联网的 fetch() 不在测试里跑（conftest 保证断网）。这里钉住的是：
  * 数字解析（"1.6万" 这种必须换算对）
  * 入库 / 筛选 / 去重 / 热度排序
  * 转发布的分支（重复发、没图、找不到）
  * 登录态文件名与 id 清洗（安全边界）
  * 平台注册表 —— 引用能力时必须都有定义
"""
from __future__ import annotations

import pytest

from core import queue
from core.collect import base, store
from core.collect.base import Item, parse_count
from core.collect import imageinfo


@pytest.fixture(autouse=True)
def _clean():
    queue.init_db()
    store.init_db()
    with queue.db() as con:
        for t in ("collected", "jobs"):
            try:
                con.execute(f"DELETE FROM {t}")
            except Exception:
                pass
    yield
    with queue.db() as con:
        for t in ("collected", "jobs"):
            try:
                con.execute(f"DELETE FROM {t}")
            except Exception:
                pass


# ── 数字解析（小红书/微博回的是 "1.6万"）──────────────────

@pytest.mark.parametrize("raw,want", [
    ("1.6万", 16000), ("3.2w", 32000), ("5.7万", 57000),
    ("1,234", 1234), ("999+", 999), ("1亿", 100000000),
    (12345, 12345), ("", 0), (None, 0), ("abc", 0), ("1.2k", 1200),
])
def test_parse_count(raw, want):
    assert parse_count(raw) == want


def test_parse_count_rejects_bool():
    assert parse_count(True) == 0


# ── 入库 / 排序 / 筛选 ─────────────────────────────────────

def _mk(pid, likes=0, comments=0, collects=0, title="t"):
    return Item(platform=pid, item_id=f"{pid}-{title}", title=title, author="a",
                likes=likes, comments=comments, collects=collects)


def test_save_and_list_sorted_by_heat():
    store.save_items([_mk("bilibili", 100), _mk("bilibili", 9000, title="hot"),
                      _mk("xiaohongshu", 500, comments=100, title="mid")])
    rows = store.list_items()
    assert len(rows) == 3
    assert rows[0]["title"] == "hot"          # 热度最高
    assert rows[0]["likes"] == 9000


def test_save_is_idempotent_and_updates():
    it = _mk("bilibili", 100, title="x")
    store.save_items([it])
    it.likes = 5000
    store.save_items([it])
    rows = store.list_items()
    assert len(rows) == 1 and rows[0]["likes"] == 5000


def test_list_filters():
    store.save_items([_mk("bilibili", 100, title="a"), _mk("bilibili", 50000, title="b"),
                      _mk("xiaohongshu", 60000, comments=5, title="c")])
    assert len(store.list_items(min_likes=10000)) == 2
    assert {r["title"] for r in store.list_items(platform="bilibili")} == {"a", "b"}
    assert len(store.list_items(min_comments=3)) == 1


def test_stats_counts():
    store.save_items([_mk("bilibili", 1), _mk("xiaohongshu", 2)])
    st = store.stats()
    assert st["total"] == 2 and st["unused"] == 2
    assert st["by_platform"] == {"bilibili": 1, "xiaohongshu": 1}


def test_prune_keeps_hottest():
    store.save_items([_mk("bilibili", i, title=f"t{i}") for i in range(10)])
    store.prune(keep=3)
    rows = store.list_items()
    assert len(rows) == 3
    assert rows[0]["likes"] == 9


def test_images_roundtrip():
    it = _mk("bilibili", 1, title="img")
    it.images = ["https://a/1.jpg", "https://a/2.jpg"]
    it.cover = "https://a/1.jpg"
    store.save_items([it])
    row = store.list_items()[0]
    assert row["images"] == ["https://a/1.jpg", "https://a/2.jpg"]


# ── 转发布 ─────────────────────────────────────────────────

def test_publish_missing_item():
    ok, msg, jid = store.publish_item("nope:nope")
    assert ok is False and jid == 0 and "不存在" in msg


def test_publish_without_image_refused():
    store.save_items([_mk("bilibili", 10, title="noimg")])
    key = store.list_items()[0]["key"]
    ok, msg, jid = store.publish_item(key)
    assert ok is False and "没有图片" in msg


def test_publish_twice_refused(monkeypatch):
    it = _mk("bilibili", 10, title="dup")
    it.images = ["https://example.invalid/x.jpg"]
    store.save_items([it])
    key = store.list_items()[0]["key"]
    monkeypatch.setattr(store, "_download", lambda *a, **k: True)
    ok, msg, jid = store.publish_item(key, text="hello")
    assert ok is True and jid > 0
    ok2, msg2, jid2 = store.publish_item(key, text="hello")
    assert ok2 is False and "已经发过" in msg2


def test_publish_download_failure(monkeypatch):
    it = _mk("bilibili", 10, title="dlfail")
    it.images = ["https://example.invalid/y.jpg"]
    store.save_items([it])
    key = store.list_items()[0]["key"]
    monkeypatch.setattr(store, "_download", lambda *a, **k: False)
    ok, msg, jid = store.publish_item(key)
    assert ok is False and "下载失败" in msg


def test_referer_matters_per_platform():
    """各平台图床都校验 Referer —— 给错了就是 403（实测踩过）。"""
    assert "bilibili.com" in store.REFERER["bilibili"]
    assert "xiaohongshu.com" in store.REFERER["xiaohongshu"]
    assert store.REFERER["bilibili"] != store.REFERER["xiaohongshu"]


# ── 登录态（安全边界）──────────────────────────────────────

def test_state_path_is_under_data_dir():
    from core import config
    p = base.state_path("xiaohongshu")
    assert str(p).startswith(str(config.DATA_DIR))
    assert p.name == "storage_state.json"


def test_state_roundtrip():
    assert base.has_state("bilibili") is False
    assert base.write_state("bilibili", {"cookies": [{"name": "x"}], "origins": []})
    assert base.has_state("bilibili") is True
    assert "1 条 cookie" in base.state_summary("bilibili")
    assert base.clear_state("bilibili")
    assert base.has_state("bilibili") is False


def test_has_state_rejects_junk():
    p = base.state_path("weibo")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}", encoding="utf-8")
    assert base.has_state("weibo") is False        # 没有 cookies
    p.write_text("not json", encoding="utf-8")
    assert base.has_state("weibo") is False


# ── 平台注册表 ─────────────────────────────────────────────

def test_platforms_have_required_meta():
    from core import collect
    for name, meta in collect.PLATFORMS.items():
        assert meta.get("label") and meta.get("glyph") and meta.get("hint"), name
        assert isinstance(meta.get("need_login"), bool), name


def test_describe_lists_every_platform():
    from core import collect
    rows = collect.describe()
    assert {r["name"] for r in rows} == set(collect.PLATFORMS)
    for r in rows:
        assert "implemented" in r and "logged_in" in r


def test_get_collector_unknown_returns_none():
    from core import collect
    assert collect.get_collector("nope") is None
    assert collect.get_collector("") is None


def test_item_heat_weights_comments():
    a = Item(platform="p", item_id="1", likes=100)
    b = Item(platform="p", item_id="2", likes=90, comments=10)
    assert b.heat() > a.heat()      # 评论权重更高


# ── 图片尺寸（竖图优先的依据）──────────────────────────────

def _png(w, h):
    import struct, zlib
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * w for _ in range(h))
    def ck(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n"
            + ck(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + ck(b"IDAT", zlib.compress(raw)) + ck(b"IEND", b""))


def _gif(w, h):
    import struct
    return b"GIF89a" + struct.pack("<HH", w, h) + b"\x00" * 20


def _jpeg(w, h):
    import struct
    return (b"\xff\xd8\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
            + b"\xff\xc0" + struct.pack(">H", 17) + b"\x08"
            + struct.pack(">HH", h, w) + b"\x03" + b"\x00" * 9 + b"\xff\xd9")


@pytest.mark.parametrize("data,want", [
    (_png(1080, 1440), (1080, 1440)),
    (_png(1200, 800), (1200, 800)),
    (_gif(400, 600), (400, 600)),
    (_jpeg(800, 600), (800, 600)),
])
def test_size_of_common_formats(data, want):
    assert imageinfo.size_of(data) == want


def test_size_of_garbage_is_none():
    assert imageinfo.size_of(b"not an image") is None
    assert imageinfo.size_of(b"") is None


def test_is_portrait_and_score():
    assert imageinfo.is_portrait((1080, 1440)) is True
    assert imageinfo.is_portrait((1200, 800)) is False
    assert imageinfo.is_portrait((800, 800)) is True     # 方图算竖图
    assert imageinfo.is_portrait(None) is False          # 认不出当横图（保守）
    assert imageinfo.ratio_score((1080, 1440)) > imageinfo.ratio_score((1200, 800))
    assert imageinfo.ratio_score(None) == 0.0


# ── 挑图：竖图优先 + 滤掉 logo ─────────────────────────────

def test_junk_filter_catches_logo():
    assert store._looks_like_junk("https://h5.sinaimg.cn/upload/weibologo.png")
    assert store._looks_like_junk("https://x.com/avatar/1.jpg")
    assert not store._looks_like_junk("https://wx1.sinaimg.cn/large/abc.jpg")
    assert not store._looks_like_junk("https://i2.hdslb.com/bfs/archive/abc.jpg")


def test_pick_best_image_prefers_portrait(monkeypatch):
    """横图在前、竖图在后时，要挑出竖图（参考号发的都是竖图）。"""
    land, port = _png(1600, 900), _png(1080, 1440)
    blobs = {"https://x/land.png": land, "https://x/port.png": port}
    monkeypatch.setattr(store, "_fetch_bytes", lambda u, *a, **k: blobs.get(u))
    item = {"platform": "xiaohongshu", "cover": "https://x/land.png",
            "images": ["https://x/land.png", "https://x/port.png"]}
    got = store.pick_best_image(item)
    assert got and got[0] == "https://x/port.png"
    assert imageinfo.is_portrait(imageinfo.size_of(got[1]))


def test_pick_best_image_skips_logo(monkeypatch):
    """URL 里带 logo 的候选要排在后面（实测微博会先命中 weibologo）。"""
    real = _png(1080, 1440)
    seen = []
    def fake(u, *a, **k):
        seen.append(u)
        return real if "weibologo" not in u else _png(200, 200)
    monkeypatch.setattr(store, "_fetch_bytes", fake)
    item = {"platform": "weibo", "cover": "https://h5.sinaimg.cn/weibologo.png",
            "images": ["https://wx1.sinaimg.cn/large/real.jpg"]}
    got = store.pick_best_image(item)
    assert got and "weibologo" not in got[0]


def test_pick_best_image_no_candidates(monkeypatch):
    monkeypatch.setattr(store, "_fetch_bytes", lambda *a, **k: None)
    assert store.pick_best_image({"platform": "x", "cover": "", "images": []}) is None
