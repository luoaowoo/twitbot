"""后端「能力标签」的回归。

背景（2026-09-26）：控制台一直看不出**哪个后端能干什么** ——
用户不知道「读日报」只有浏览器后端能做、X API 的媒体上传在免费层常被拒。
后端类本身是冻结的（base.py 不能加字段），所以能力表写在集成层
web/server.py 的 BACKEND_CAPS 里。

这组用例把这个事实钉死：标签必须存在、状态必须准确，
而且**不能把浏览器独有的能力错标到 X API 上**。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web import server as web_server

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "web" / "static" / "index.html"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("WEB_PASSWORD", "-")
    monkeypatch.setattr(web_server, "backends_view", web_server.backends_view)
    web_server.invalidate_backends_cache()
    return TestClient(web_server.create_app())


def _caps(name: str) -> dict[str, str]:
    return {c["t"]: c["s"] for c in web_server.BACKEND_CAPS.get(name, [])}


# ── 能力表本身 ─────────────────────────────────────────────

def test_both_backends_have_caps():
    for n in ("x_api", "browser"):
        assert web_server.BACKEND_CAPS.get(n), f"{n} 没有能力表"


def test_cap_states_are_valid():
    for n, rows in web_server.BACKEND_CAPS.items():
        for c in rows:
            assert set(c) == {"t", "s"}, (n, c)
            assert c["s"] in ("ok", "warn", "no"), (n, c)
            assert c["t"].strip(), (n, c)


def test_browser_can_read_analytics_but_x_api_cannot():
    """读日报只有浏览器能做 —— 这是日报功能的前提。"""
    assert _caps("browser")["读日报"] == "ok"
    assert _caps("x_api")["读日报"] == "no"


def test_x_api_media_is_marked_with_caveat():
    """X API 免费层的 media/upload 常被 403 —— 必须标成「有前提」而不是「支持」。"""
    x = _caps("x_api")
    assert x["发图片"] == "warn"
    assert x["发视频"] == "warn"
    assert _caps("browser")["发图片"] == "ok"
    assert _caps("browser")["发视频"] == "ok"


def test_text_and_tweet_ops_are_ok_on_both():
    for n in ("x_api", "browser"):
        c = _caps(n)
        for k in ("发文字", "转帖", "引用", "评论"):
            assert c.get(k) == "ok", (n, k, c.get(k))


def test_glyph_and_hint_present_for_known_backends():
    for n in ("x_api", "browser"):
        assert web_server.BACKEND_GLYPH.get(n)
        assert web_server.BACKEND_HINT.get(n)


# ── 接口把能力带给前端 ─────────────────────────────────────

def test_status_payload_backends_carry_caps(client):
    rows = client.get("/api/status").json()["backends"]
    assert rows, "至少要有后端"
    for b in rows:
        assert "caps" in b and isinstance(b["caps"], list)
        assert "glyph" in b and b["glyph"]
        assert "hint" in b
    by = {b["name"]: b for b in rows}
    assert {c["t"] for c in by["browser"]["caps"]} >= {"读日报", "发视频"}
    assert {c["t"] for c in by["x_api"]["caps"]} >= {"发文字", "转帖"}


def test_every_registered_backend_has_a_cap_entry():
    """registry 里有的后端，能力表里也必须有 —— 否则卡片会缺一块。"""
    from core.backends import registry
    for name in registry.REGISTRY:
        assert name in web_server.BACKEND_CAPS, f"{name} 没写能力表"


# ── 前端渲染 ───────────────────────────────────────────────

def test_render_backends_uses_new_fields():
    html = INDEX.read_text(encoding="utf-8")
    i = html.index("function renderBackends")
    body = html[i:html.index("function renderSettings")]
    for token in ("b.caps", "b.glyph", "b.hint", "bkstatus", "bkuse", "cap "):
        assert token in body, f"renderBackends 里没用到 {token}"


def test_backend_card_css_present():
    html = INDEX.read_text(encoding="utf-8")
    for sel in (".bkhead .glyph", ".bkstatus", ".caps", ".cap.warn", ".cap.no", ".bkuse"):
        assert sel in html, f"缺少样式 {sel}"


def test_no_stale_backend_badge_vars():
    """旧的 cls/txt 局部变量必须清干净（留着就是死代码）。"""
    html = INDEX.read_text(encoding="utf-8")
    i = html.index("function renderBackends")
    body = html[i:html.index("function renderSettings")]
    assert not re.search(r"\bcls\b", body)
    assert not re.search(r"\btxt\b", body)

