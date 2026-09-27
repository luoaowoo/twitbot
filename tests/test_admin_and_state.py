"""「上传登录态 JSON」+「管理员支持 @用户名」的离线测试（不联网）。"""
from __future__ import annotations

import asyncio
import io
import json
import urllib.error

import pytest
from fastapi.testclient import TestClient

import bot
from core import analytics, queue, tg_contacts
from web import server as web_server


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """关掉控制台密码门（服务器版才有；桌面版忽略）。"""
    monkeypatch.setenv("WEB_PASSWORD", "-")
    queue.init_db()
    analytics.init_db()
    tg_contacts.init_db()
    with queue.db() as con:
        for t in ("settings", "x_config", "tg_contacts", "tg_admins", "tg_signups"):
            try:
                con.execute(f"DELETE FROM {t}")
            except Exception:
                pass
    yield
    with queue.db() as con:          # 跑完也清：免得影响后面 test_tgmanager 的鉴权用例
        for t in ("settings", "x_config", "tg_contacts", "tg_admins", "tg_signups"):
            try:
                con.execute(f"DELETE FROM {t}")
            except Exception:
                pass


@pytest.fixture()
def client():
    return TestClient(web_server.create_app())


# ── 输入切分 / 解析 ────────────────────────────────────────

def test_split_id_tokens_handles_mixed_input():
    assert web_server.split_id_tokens("@a, 123，@b 456;@c") == \
        ["@a", "123", "@b", "456", "@c"]
    assert web_server.split_id_tokens("") == []
    assert web_server.split_id_tokens("   ") == []


def test_resolve_tg_chat_numeric_passthrough(monkeypatch):
    # 数字 id 直接返回，绝不联网
    monkeypatch.setattr(web_server.urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("数字 id 不该联网"))
    r = web_server.resolve_tg_chat("7988954881")
    assert r["ok"] is True and r["id"] == 7988954881
    assert web_server.resolve_tg_chat("-1001234567890")["id"] == -1001234567890


def test_resolve_tg_chat_empty():
    assert web_server.resolve_tg_chat("")["ok"] is False


def test_resolve_tg_chat_without_token(monkeypatch):
    monkeypatch.setattr(web_server, "_tg_token", lambda: "")
    r = web_server.resolve_tg_chat("@someone")
    assert r["ok"] is False and "token" in r["error"]


def test_resolve_tg_chat_username_ok(monkeypatch):
    monkeypatch.setattr(web_server, "_tg_token", lambda: "123456:FAKE")
    payload = {"ok": True, "result": {"id": 7988954881, "first_name": "洛嗷呜",
                                      "last_name": "luoaowoo", "username": "luoaowoo",
                                      "type": "private"}}

    class _Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(url, timeout=10):
        assert "getChat" in url and "%40luoaowoo" in url     # @ 被编码了
        return _Resp(json.dumps(payload).encode())

    monkeypatch.setattr(web_server.urllib.request, "urlopen", fake_urlopen)
    r = web_server.resolve_tg_chat("@luoaowoo")
    assert r["ok"] is True and r["id"] == 7988954881
    assert r["title"] == "洛嗷呜 luoaowoo" and r["username"] == "luoaowoo"


def test_resolve_tg_chat_never_leaks_token_in_error(monkeypatch):
    """异常信息里绝不能出现 token（它会出现在请求 URL 里）。"""
    monkeypatch.setattr(web_server, "_tg_token", lambda: "123456:SUPERSECRET")

    def boom(url, timeout=10):
        raise urllib.error.URLError(f"failed to reach https://api.telegram.org/bot123456:SUPERSECRET/getChat")

    monkeypatch.setattr(web_server.urllib.request, "urlopen", boom)
    r = web_server.resolve_tg_chat("@x")
    assert r["ok"] is False
    assert "SUPERSECRET" not in json.dumps(r, ensure_ascii=False)


def test_resolve_tg_chat_telegram_says_not_found(monkeypatch):
    monkeypatch.setattr(web_server, "_tg_token", lambda: "1:FAKE")

    def err(url, timeout=10):
        raise urllib.error.HTTPError(url, 400, "Bad Request", {},
                                     io.BytesIO(json.dumps(
                                         {"ok": False, "description": "chat not found"}
                                     ).encode()))

    monkeypatch.setattr(web_server.urllib.request, "urlopen", err)
    r = web_server.resolve_tg_chat("@nobody")
    assert r["ok"] is False and "chat not found" in r["error"]
    assert "FAKE" not in json.dumps(r, ensure_ascii=False)


# ── /api/settings：管理员允许 @用户名 ──────────────────────

def test_settings_saves_plain_numeric_ids(client):
    r = client.post("/api/settings", json={"tg_allowed_users": "111, 222"})
    assert r.status_code == 200
    assert r.json()["saved"]["tg_allowed_users"] == "111,222"


def test_settings_resolves_username_to_id(client, monkeypatch):
    monkeypatch.setattr(web_server, "resolve_tg_chat",
                        lambda q: ({"ok": True, "id": 7988954881, "title": "洛嗷呜"}
                                   if q.lstrip("@") == "luoaowoo"
                                   else {"ok": False, "error": "没找到"}))
    r = client.post("/api/settings", json={"tg_allowed_users": "@luoaowoo, 111"})
    assert r.status_code == 200
    assert r.json()["saved"]["tg_allowed_users"] == "7988954881,111"


def test_settings_dedupes_resolved_ids(client, monkeypatch):
    monkeypatch.setattr(web_server, "resolve_tg_chat",
                        lambda q: {"ok": True, "id": 42})
    r = client.post("/api/settings", json={"tg_allowed_users": "@a,@b,42"})
    assert r.status_code == 200
    assert r.json()["saved"]["tg_allowed_users"] == "42"


def test_settings_rejects_unresolvable_username(client, monkeypatch):
    monkeypatch.setattr(web_server, "resolve_tg_chat",
                        lambda q: {"ok": False, "error": "chat not found"})
    r = client.post("/api/settings", json={"tg_allowed_users": "@nobody"})
    assert r.status_code == 400
    assert "chat not found" in r.json()["error"]


def test_settings_clearing_whitelist_is_allowed(client):
    r = client.post("/api/settings", json={"tg_allowed_users": ""})
    assert r.status_code == 200
    assert r.json()["saved"]["tg_allowed_users"] == ""


# ── /api/tg/resolve ────────────────────────────────────────

def test_tg_resolve_endpoint(client, monkeypatch):
    monkeypatch.setattr(web_server, "resolve_tg_chat",
                        lambda q: {"ok": True, "id": 7, "title": q})
    r = client.post("/api/tg/resolve", json={"query": "@abc"})
    assert r.status_code == 200 and r.json()["id"] == 7


# ── 上传登录态 JSON ────────────────────────────────────────

def _state(extra_names=()):
    cookies = [{"name": "auth_token", "value": "x", "domain": ".x.com", "path": "/"},
               {"name": "ct0", "value": "y", "domain": ".x.com", "path": "/"}]
    cookies += [{"name": n, "value": "z", "domain": ".x.com", "path": "/"}
                for n in extra_names]
    return {"cookies": cookies, "origins": []}


def test_upload_state_saves_file(client, tmp_path, monkeypatch):
    from core.backends import browser as browser_mod
    target = tmp_path / "browser" / "storage_state.json"
    monkeypatch.setattr(browser_mod.BrowserBackend, "state_path", lambda self: target)
    body = json.dumps(_state(["twid"])).encode()
    r = client.post("/api/browser/state/upload",
                    files={"file": ("storage_state.json", body, "application/json")})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and "3 条 cookie" in r.json()["message"]
    assert target.exists()
    assert json.loads(target.read_text(encoding="utf-8"))["cookies"][0]["name"] == "auth_token"


def test_upload_state_rejects_bad_json(client, tmp_path, monkeypatch):
    from core.backends import browser as browser_mod
    monkeypatch.setattr(browser_mod.BrowserBackend, "state_path",
                        lambda self: tmp_path / "s.json")
    r = client.post("/api/browser/state/upload",
                    files={"file": ("x.json", b"not json", "application/json")})
    assert r.status_code == 400 and "JSON" in r.json()["error"]


def test_upload_state_rejects_missing_auth_token(client, tmp_path, monkeypatch):
    from core.backends import browser as browser_mod
    monkeypatch.setattr(browser_mod.BrowserBackend, "state_path",
                        lambda self: tmp_path / "s.json")
    body = json.dumps({"cookies": [{"name": "ct0", "value": "y"}]}).encode()
    r = client.post("/api/browser/state/upload",
                    files={"file": ("x.json", body, "application/json")})
    assert r.status_code == 400 and "auth_token" in r.json()["error"]


def test_upload_state_rejects_wrong_shape(client, tmp_path, monkeypatch):
    from core.backends import browser as browser_mod
    monkeypatch.setattr(browser_mod.BrowserBackend, "state_path",
                        lambda self: tmp_path / "s.json")
    r = client.post("/api/browser/state/upload",
                    files={"file": ("x.json", b'{"foo":1}', "application/json")})
    assert r.status_code == 400 and "cookies" in r.json()["error"]


def test_upload_state_rejects_empty(client, tmp_path, monkeypatch):
    from core.backends import browser as browser_mod
    monkeypatch.setattr(browser_mod.BrowserBackend, "state_path",
                        lambda self: tmp_path / "s.json")
    r = client.post("/api/browser/state/upload",
                    files={"file": ("x.json", b"", "application/json")})
    assert r.status_code == 400


def test_get_state_reports_existence(client, tmp_path, monkeypatch):
    from core.backends import browser as browser_mod
    target = tmp_path / "s.json"
    monkeypatch.setattr(browser_mod.BrowserBackend, "state_path", lambda self: target)
    r = client.get("/api/browser/state")
    assert r.status_code == 200 and r.json()["exists"] is False

    target.write_text(json.dumps(_state()), encoding="utf-8")
    r = client.get("/api/browser/state")
    body = r.json()
    assert body["exists"] is True and body["has_auth"] is True
    assert "auth_token" in body["cookie_names"]
    assert "value" not in json.dumps(body)        # 绝不回传 cookie 内容


# ── 机器人：用 @用户名 加管理员 ────────────────────────────

class _Chat:
    def __init__(self, cid=7988954881, name="洛嗷呜 luoaowoo"):
        self.id = cid
        self.full_name = name
        self.username = "luoaowoo"


class _Bot:
    def __init__(self, fail=False):
        self.fail = fail
        self.asked: list[str] = []

    async def get_chat(self, handle):
        self.asked.append(handle)
        if self.fail:
            raise RuntimeError("chat not found")
        return _Chat()


class _Msg:
    def __init__(self, bot_obj=None):
        self._bot = bot_obj
        self.replies: list[str] = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)

    def get_bot(self):
        return self._bot


def _run(coro):
    return asyncio.run(coro)


def test_add_admin_by_numeric_id():
    msg = _Msg()
    _run(bot._add_admin_by_query(msg, "7988954881"))
    assert "已加为管理员" in msg.replies[-1]
    from core import settings
    assert settings.get("tg_allowed_users") == "7988954881"


def test_add_admin_by_username_resolves():
    b = _Bot()
    msg = _Msg(b)
    _run(bot._add_admin_by_query(msg, "@luoaowoo"))
    assert b.asked == ["@luoaowoo"]
    assert "洛嗷呜 luoaowoo（7988954881）" in msg.replies[-1]
    from core import settings
    assert settings.get("tg_allowed_users") == "7988954881"


def test_add_admin_username_unresolvable():
    msg = _Msg(_Bot(fail=True))
    _run(bot._add_admin_by_query(msg, "@nobody"))
    assert "没能把 @nobody 换算成 user id" in msg.replies[-1]
    assert "先给机器人发一句 /start" in msg.replies[-1]
    from core import settings
    assert settings.get("tg_allowed_users") == ""


def test_add_admin_twice_is_idempotent():
    msg = _Msg()
    _run(bot._add_admin_by_query(msg, "42"))
    _run(bot._add_admin_by_query(msg, "42"))
    assert "已经是管理员" in msg.replies[-1]


def test_remove_admin():
    msg = _Msg()
    _run(bot._add_admin_by_query(msg, "42"))
    _run(bot._add_admin_by_query(msg, "42", remove=True))
    assert "已移除管理员" in msg.replies[-1]
    from core import settings
    assert settings.get("tg_allowed_users") == ""


def test_remove_admin_not_present():
    msg = _Msg()
    _run(bot._add_admin_by_query(msg, "999", remove=True))
    assert "不在管理员列表里" in msg.replies[-1]


def test_add_admin_empty_usage():
    msg = _Msg()
    _run(bot._add_admin_by_query(msg, ""))
    assert "用法" in msg.replies[-1]


def test_admin_command_routing():
    msg = _Msg()
    assert _run(bot._maybe_admin_command(msg, "添加管理员 42")) is True
    assert _run(bot._maybe_admin_command(msg, "/addadmin 43")) is True
    assert _run(bot._maybe_admin_command(msg, "删除管理员 42")) is True
    assert _run(bot._maybe_admin_command(msg, "日报")) is False
    assert _run(bot._maybe_admin_command(msg, "今天发点啥")) is False


# ── 「见过的人」名册（core.tg_contacts）────────────────────

def test_contacts_remember_and_lookup_by_username():
    assert tg_contacts.remember(7988954881, "luoaowoo", "洛嗷呜", 7988954881, "private")
    hit = tg_contacts.lookup("@luoaowoo")
    assert hit and hit["id"] == 7988954881
    assert tg_contacts.lookup("LUOAOWOO")["id"] == 7988954881     # 大小写不敏感
    assert tg_contacts.lookup("7988954881")["name"] == "洛嗷呜"
    assert tg_contacts.lookup("@nobody") is None


def test_contacts_rejects_invalid_id():
    assert tg_contacts.remember(0, "x", "y") is False
    assert tg_contacts.remember(None, "x", "y") is False
    assert tg_contacts.remember("abc", "x", "y") is False


def test_contacts_remember_updates_username():
    tg_contacts.remember(5, "old", "老王")
    tg_contacts.remember(5, "new", "老王")
    assert tg_contacts.lookup("@new")["id"] == 5
    assert tg_contacts.lookup("@old") is None
    assert len(tg_contacts.recent()) == 1


def test_contacts_recent_and_display():
    tg_contacts.remember(1, "a", "甲")
    tg_contacts.remember(2, "b", "乙")
    rows = tg_contacts.recent(10)
    assert {r["id"] for r in rows} == {1, 2}
    assert tg_contacts.display({"id": 7, "name": "丙", "username": "bing"}) == \
        "丙（@bing · 7）"
    assert tg_contacts.display({"id": 7}) == "7"


def test_resolve_uses_local_contacts_without_network(monkeypatch):
    """名册里有就直接用，绝不去打 Telegram（私聊用户 getChat 本来就查不到）。"""
    tg_contacts.remember(7988954881, "luoaowoo", "洛嗷呜", 7988954881, "private")
    monkeypatch.setattr(web_server.urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("命中名册就不该联网"))
    r = web_server.resolve_tg_chat("@luoaowoo")
    assert r["ok"] is True and r["id"] == 7988954881
    assert r["source"] == "contacts"


def test_resolve_falls_back_to_api_when_not_in_contacts(monkeypatch):
    monkeypatch.setattr(web_server, "_tg_token", lambda: "1:FAKE")
    payload = {"ok": True, "result": {"id": 42, "title": "某频道", "type": "channel"}}

    class _Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(web_server.urllib.request, "urlopen",
                        lambda url, timeout=10: _Resp(json.dumps(payload).encode()))
    r = web_server.resolve_tg_chat("@somechannel")
    assert r["ok"] is True and r["id"] == 42
    assert "source" not in r


def test_api_tg_contacts_endpoint(client):
    tg_contacts.remember(7988954881, "luoaowoo", "洛嗷呜", 7988954881, "private")
    r = client.get("/api/tg/contacts")
    assert r.status_code == 200
    rows = r.json()["contacts"]
    assert rows and rows[0]["id"] == 7988954881 and rows[0]["username"] == "luoaowoo"


def test_add_admin_by_username_uses_contacts():
    """名册里有的人，不加 get_bot() 也能加管理员。"""
    tg_contacts.remember(7988954881, "luoaowoo", "洛嗷呜", 7988954881, "private")
    msg = _Msg(bot_obj=None)                  # 故意不给 bot：不该被调用
    _run(bot._add_admin_by_query(msg, "@luoaowoo"))
    assert "已加为管理员" in msg.replies[-1]
    from core import settings
    assert settings.get("tg_allowed_users") == "7988954881"
