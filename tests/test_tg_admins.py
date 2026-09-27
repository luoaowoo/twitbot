"""管理员列表 + /sign 申请审批的离线测试（不联网）。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import bot
from web import server as web_server
from core import queue, settings, tg_admins

UTC = timezone.utc


@pytest.fixture(autouse=True)
def _clean():
    queue.init_db()
    tg_admins.init_db()
    _wipe()
    yield
    _wipe()          # 跑完也要清干净：否则后面 test_tgmanager 的鉴权用例会串台


def _wipe():
    with queue.db() as con:
        for t in ("tg_admins", "tg_signups", "tg_contacts", "settings", "x_config"):
            try:
                con.execute(f"DELETE FROM {t}")
            except Exception:
                pass


# ── 列表本身 ───────────────────────────────────────────────

def test_add_and_list():
    assert tg_admins.add_admin(101, "a", "甲", source="owner")
    assert tg_admins.add_admin(202, "b", "乙")
    rows = tg_admins.admins()
    assert [r["user_id"] for r in rows] == [101, 202]
    assert tg_admins.is_admin(101) and tg_admins.is_admin(202)
    assert not tg_admins.is_admin(999)
    assert tg_admins.admin_ids() == {101, 202}


def test_add_is_idempotent_and_updates_name():
    tg_admins.add_admin(101, "old", "老名")
    tg_admins.add_admin(101, "new", "新名")
    rows = tg_admins.admins()
    assert len(rows) == 1
    assert rows[0]["username"] == "new" and rows[0]["name"] == "新名"


def test_add_rejects_bad_id():
    assert tg_admins.add_admin(0) is False
    assert tg_admins.add_admin(None) is False
    assert tg_admins.add_admin("abc") is False


def test_owner_defaults_to_first_admin():
    tg_admins.add_admin(101, "a", "甲")
    tg_admins.add_admin(202, "b", "乙")
    assert tg_admins.owner_id() == 101
    assert tg_admins.is_owner(101) and not tg_admins.is_owner(202)
    assert tg_admins.admins()[0]["user_id"] == 101
    assert tg_admins.admins()[0]["is_owner"] is True


def test_owner_can_be_changed():
    tg_admins.add_admin(101, "a", "甲")
    tg_admins.add_admin(202, "b", "乙")
    assert tg_admins.set_owner(202)
    assert tg_admins.owner_id() == 202 and tg_admins.is_owner(202)
    # 传 0 = 回到「第一个管理员」
    assert tg_admins.set_owner(0)
    assert tg_admins.owner_id() == 101


def test_remove_admin():
    tg_admins.add_admin(101)
    tg_admins.add_admin(202)
    assert tg_admins.remove_admin(202)
    assert tg_admins.admin_ids() == {101}


def test_remove_last_admin_is_blocked():
    """不能把最后一个管理员删掉 —— 删了就没人是所有者、没人能批申请。"""
    tg_admins.add_admin(101)
    assert tg_admins.remove_admin(101) is False
    assert tg_admins.admin_ids() == {101}


def test_ensure_seed_migrates_legacy_whitelist():
    """升级前的 settings.tg_allowed_users（逗号串）要自动搬进列表。"""
    settings.set_many({"tg_allowed_users": "7988954881, 42"})
    assert tg_admins.ensure_seed() == 2
    assert tg_admins.admin_ids() == {7988954881, 42}
    assert tg_admins.owner_id() == 7988954881      # 第一个搬进来的当所有者
    assert tg_admins.ensure_seed() == 0            # 幂等


def test_ensure_seed_skips_when_already_populated():
    tg_admins.add_admin(1)
    settings.set_many({"tg_allowed_users": "999"})
    assert tg_admins.ensure_seed() == 0
    assert tg_admins.admin_ids() == {1}


def test_owner_id_triggers_migration():
    """只读 owner_id() 也要能把老白名单迁进来 —— 否则升级后没人能审批。"""
    settings.set_many({"tg_allowed_users": "7988954881"})
    assert tg_admins.owner_id() == 7988954881
    assert tg_admins.is_admin(7988954881)


def test_no_infinite_recursion_between_owner_and_admins():
    """owner_id() -> ensure_seed() -> admins() -> owner_id() 不能死循环。"""
    tg_admins.add_admin(5)
    for _ in range(3):
        tg_admins.admins()
        tg_admins.owner_id()
        tg_admins.admin_ids()
    assert tg_admins.owner_id() == 5


def test_remove_last_admin_blocked_then_ok_after_second():
    settings.set_many({"tg_allowed_users": "1"})
    assert tg_admins.owner_id() == 1
    assert tg_admins.remove_admin(1) is False          # 只剩一个：不让删
    assert tg_admins.add_admin(2) is True
    assert tg_admins.remove_admin(1) is True           # 有两个了就能删
    assert tg_admins.admin_ids() == {2}


# ── 申请 / 一个月一次 ──────────────────────────────────────

def test_apply_then_pending_blocks_second_apply():
    ok, why, row = tg_admins.create_signup(500, "wu", "吴")
    assert ok and row["status"] == "pending"
    ok2, why2, _ = tg_admins.create_signup(500, "wu", "吴")
    assert ok2 is False and "等审批" in why2
    assert len(tg_admins.pending_signups()) == 1


def test_admin_cannot_apply():
    tg_admins.add_admin(500)
    ok, why, _ = tg_admins.create_signup(500)
    assert ok is False and "已经是管理员" in why


def test_monthly_cooldown_after_rejection():
    ok, _, row = tg_admins.create_signup(500, "wu", "吴")
    assert ok
    assert tg_admins.decide(row["id"], False, decided_by=1)[0]
    ok2, why2, _ = tg_admins.create_signup(500, "wu", "吴")
    assert ok2 is False and "一个月只能申请一次" in why2


def test_cooldown_expires_after_30_days():
    ok, _, row = tg_admins.create_signup(500, "wu", "吴")
    assert ok
    tg_admins.decide(row["id"], False, decided_by=1)
    old = (datetime.now(UTC) - timedelta(days=31)).isoformat(timespec="seconds")
    with queue.db() as con:
        con.execute("UPDATE tg_signups SET created_at=? WHERE id=?", (old, row["id"]))
    ok2, why2, _ = tg_admins.create_signup(500, "wu", "吴")
    assert ok2 is True, why2


# ── 审批 ───────────────────────────────────────────────────

def test_approve_adds_admin():
    ok, _, row = tg_admins.create_signup(500, "wu", "吴")
    assert ok
    ok2, note, decided = tg_admins.decide(row["id"], True, decided_by=1)
    assert ok2 is True and decided["status"] == "approved"
    assert tg_admins.is_admin(500)
    assert tg_admins.get_admin(500)["name"] == "吴"
    assert tg_admins.pending_signups() == []


def test_reject_does_not_add_admin():
    ok, _, row = tg_admins.create_signup(500, "wu", "吴")
    assert ok
    ok2, _, decided = tg_admins.decide(row["id"], False, decided_by=1)
    assert ok2 is True and decided["status"] == "rejected"
    assert not tg_admins.is_admin(500)


def test_decide_twice_is_refused():
    _, _, row = tg_admins.create_signup(500)
    assert tg_admins.decide(row["id"], True, decided_by=1)[0] is True
    ok2, why2, _ = tg_admins.decide(row["id"], True, decided_by=1)
    assert ok2 is False and "处理过" in why2


def test_decide_unknown_id():
    ok, why, _ = tg_admins.decide(9999, True)
    assert ok is False and "找不到" in why


# ── 机器人侧：/sign、非管理员提示、鉴权 ────────────────────

class _User:
    def __init__(self, uid, username="", full_name=""):
        self.id = uid
        self.username = username
        self.full_name = full_name


class _Chat:
    def __init__(self, cid, ctype="private"):
        self.id = cid
        self.type = ctype


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))


class _Msg:
    def __init__(self, user, chat, text="", bot_obj=None):
        self.from_user = user
        self.chat = chat
        self.text = text
        self._bot = bot_obj or _Bot()
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw))

    def get_bot(self):
        return self._bot


class _Update:
    def __init__(self, msg):
        self.effective_message = msg
        self.effective_user = msg.from_user
        self.effective_chat = msg.chat


def _run(coro):
    return asyncio.run(coro)


def test_sign_creates_signup_and_notifies_owner():
    tg_admins.add_admin(101, "owner", "老板")
    b = _Bot()
    msg = _Msg(_User(500, "wu", "吴"), _Chat(500), "/sign", b)
    up = _Update(msg)
    assert _run(bot._maybe_sign_command(up, msg)) is True
    assert any("申请已提交" in t for t, _ in msg.replies)
    assert len(tg_admins.pending_signups()) == 1
    # 通知发给了所有者
    assert b.sent and b.sent[0][0] == 101
    assert "新的管理员申请" in b.sent[0][1]
    btns = [x.callback_data for row in b.sent[0][2]["reply_markup"].inline_keyboard for x in row]
    assert any(x.startswith("sg:ok:") for x in btns)
    assert any(x.startswith("sg:no:") for x in btns)


def test_sign_when_already_admin():
    tg_admins.add_admin(500, "wu", "吴")
    msg = _Msg(_User(500, "wu", "吴"), _Chat(500), "/sign")
    assert _run(bot._maybe_sign_command(_Update(msg), msg)) is True
    assert any("已经是管理员" in t for t, _ in msg.replies)
    assert tg_admins.pending_signups() == []


def test_sign_twice_in_a_month():
    tg_admins.add_admin(101)
    msg = _Msg(_User(500, "wu", "吴"), _Chat(500), "/sign")
    _run(bot._maybe_sign_command(_Update(msg), msg))
    msg2 = _Msg(_User(500, "wu", "吴"), _Chat(500), "/sign")
    _run(bot._maybe_sign_command(_Update(msg2), msg2))
    assert any("等审批" in t for t, _ in msg2.replies)


def test_bare_sign_only_for_non_admin():
    """裸写 "sign" 对非管理员当命令；管理员可能真想发这个内容，不抢。"""
    tg_admins.add_admin(500)
    msg = _Msg(_User(500), _Chat(500), "sign")
    assert _run(bot._maybe_sign_command(_Update(msg), msg)) is False
    msg2 = _Msg(_User(777), _Chat(777), "sign")
    assert _run(bot._maybe_sign_command(_Update(msg2), msg2)) is True


def test_non_sign_message_is_not_a_command():
    msg = _Msg(_User(500), _Chat(500), "帮我发条推")
    assert _run(bot._maybe_sign_command(_Update(msg), msg)) is False


def test_hint_not_admin_in_private_chat():
    msg = _Msg(_User(500), _Chat(500, "private"))
    _run(bot._hint_not_admin(_Update(msg), "不在管理员列表里"))
    assert any("/sign" in t for t, _ in msg.replies)


def test_hint_not_admin_silent_in_group():
    msg = _Msg(_User(500), _Chat(-100, "supergroup"))
    _run(bot._hint_not_admin(_Update(msg), "不在管理员列表里"))
    assert msg.replies == []


def test_allowed_users_effective_uses_admin_list():
    tg_admins.add_admin(101)
    tg_admins.add_admin(202)
    assert bot.allowed_users_effective() == {101, 202}


def test_allowed_users_effective_migrates_legacy():
    settings.set_many({"tg_allowed_users": "555"})
    assert bot.allowed_users_effective() == {555}


def test_authorized_blocks_non_admin():
    tg_admins.add_admin(101)
    msg = _Msg(_User(500), _Chat(500))
    ok, why = _run(bot._authorized(_Update(msg)))
    assert ok is False and "管理员列表" in why


def test_authorized_allows_admin():
    tg_admins.add_admin(101)
    msg = _Msg(_User(101), _Chat(101))
    ok, why = _run(bot._authorized(_Update(msg)))
    assert ok is True


def test_stats():
    tg_admins.add_admin(101)
    tg_admins.create_signup(500)
    st = tg_admins.stats()
    assert st["admins"] == 1 and st["pending"] == 1 and st["owner_id"] == 101


# ── 控制台接口 ─────────────────────────────────────────────

@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("WEB_PASSWORD", "-")      # 关掉服务器版的密码门
    return TestClient(web_server.create_app())


def test_api_lists_admins_and_pending(client):
    tg_admins.add_admin(101, "owner", "老板")
    tg_admins.create_signup(500, "wu", "吴")
    d = client.get("/api/tg/admins").json()
    assert d["ok"] is True
    assert d["admins"][0]["user_id"] == 101 and d["admins"][0]["is_owner"] is True
    assert d["pending"][0]["user_id"] == 500
    assert d["owner_id"] == 101


def test_api_add_admin_by_username(client, monkeypatch):
    tg_admins.add_admin(101, "owner", "老板")     # 先有个所有者
    monkeypatch.setattr(web_server, "resolve_tg_chat",
                        lambda q: {"ok": True, "id": 500, "username": "wu", "title": "吴"})
    r = client.post("/api/tg/admins/add", json={"query": "@wu"})
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] == 500
    assert tg_admins.is_admin(500)


def test_api_add_admin_unresolvable(client, monkeypatch):
    monkeypatch.setattr(web_server, "resolve_tg_chat",
                        lambda q: {"ok": False, "error": "chat not found"})
    r = client.post("/api/tg/admins/add", json={"query": "@nobody"})
    assert r.status_code == 400 and "chat not found" in r.json()["error"]


def test_api_add_admin_empty_query(client):
    assert client.post("/api/tg/admins/add", json={"query": "  "}).status_code == 400


def test_api_remove_admin(client):
    tg_admins.add_admin(101)
    tg_admins.add_admin(202)
    assert client.post("/api/tg/admins/remove", json={"user_id": 202}).status_code == 200
    assert tg_admins.admin_ids() == {101}


def test_api_remove_last_admin_refused(client):
    tg_admins.add_admin(101)
    r = client.post("/api/tg/admins/remove", json={"user_id": 101})
    assert r.status_code == 400 and "最后一个" in r.json()["error"]


def test_api_set_owner(client):
    tg_admins.add_admin(101)
    tg_admins.add_admin(202)
    assert client.post("/api/tg/admins/owner", json={"user_id": 202}).status_code == 200
    assert tg_admins.owner_id() == 202


def test_api_set_owner_requires_admin(client):
    tg_admins.add_admin(101)
    r = client.post("/api/tg/admins/owner", json={"user_id": 999})
    assert r.status_code == 400 and "必须是" in r.json()["error"]


def test_api_approve_notifies_applicant(client, monkeypatch):
    tg_admins.add_admin(101)
    _, _, row = tg_admins.create_signup(500, "wu", "吴")
    called = []
    monkeypatch.setattr(tg_admins, "notify_user",
                        lambda uid, text: called.append((uid, text)) or True)
    r = client.post("/api/tg/admins/decide",
                    json={"signup_id": row["id"], "approve": True})
    assert r.status_code == 200, r.text
    assert tg_admins.is_admin(500)
    assert called and called[0][0] == 500 and "已通过" in called[0][1]


def test_api_reject_notifies_applicant(client, monkeypatch):
    tg_admins.add_admin(101)
    _, _, row = tg_admins.create_signup(500, "wu", "吴")
    called = []
    monkeypatch.setattr(tg_admins, "notify_user",
                        lambda uid, text: called.append((uid, text)) or True)
    r = client.post("/api/tg/admins/decide",
                    json={"signup_id": row["id"], "approve": False})
    assert r.status_code == 200
    assert not tg_admins.is_admin(500)
    assert called and "未通过" in called[0][1]


def test_api_decide_twice_refused(client, monkeypatch):
    tg_admins.add_admin(101)
    _, _, row = tg_admins.create_signup(500)
    monkeypatch.setattr(tg_admins, "notify_user", lambda uid, text: True)
    assert client.post("/api/tg/admins/decide",
                       json={"signup_id": row["id"], "approve": True}).status_code == 200
    r = client.post("/api/tg/admins/decide",
                    json={"signup_id": row["id"], "approve": True})
    assert r.status_code == 400


# ── 命令鉴权 + /sign 可达性（回归）──────────────────────────

def test_command_handlers_are_guarded():
    """所有命令都要过 _require_admin；**只有 /sign 例外**。

    回归的病根：PTB 的 CommandHandler 先于 on_message 触发，
    不包鉴权的话非管理员发 /status、/queue 就能拿到内容。
    """
    import inspect
    src = inspect.getsource(bot.build_application)
    lines = [l.strip() for l in src.splitlines() if "add_handler(CommandHandler" in l]
    assert len(lines) == 10
    open_cmds = [l for l in lines if "_require_admin" not in l]
    assert len(open_cmds) == 1 and '"sign"' in open_cmds[0], open_cmds


def test_message_handler_does_not_filter_commands():
    """MessageHandler **不能**带 ~filters.COMMAND —— 否则 /sign 到不了处理函数。"""
    import inspect
    src = inspect.getsource(bot.build_application)
    block = src[src.index("MessageHandler"):]
    assert "~filters.COMMAND" not in block


def test_on_message_ignores_slash_commands():
    """命令要显式跳过 —— 否则管理员发 /status 会被当成推文正文投料。"""
    import inspect
    assert 'startswith("/")' in inspect.getsource(bot._on_message_inner)


def test_cmd_sign_creates_signup_and_notifies_owner():
    tg_admins.add_admin(101, "owner", "老板")
    b = _Bot()
    msg = _Msg(_User(500, "wu", "吴"), _Chat(500), "/sign", b)
    _run(bot.cmd_sign(_Update(msg), None))
    assert any("申请已提交" in t for t, _ in msg.replies)
    assert len(tg_admins.pending_signups()) == 1
    assert b.sent and b.sent[0][0] == 101


def test_require_admin_blocks_non_admin():
    tg_admins.add_admin(101)
    called = []

    @bot._require_admin
    async def _inner(update, ctx):
        called.append(True)

    msg = _Msg(_User(500), _Chat(500), "/status")
    _run(_inner(_Update(msg), None))
    assert called == []
    assert any("/sign" in t for t, _ in msg.replies)


def test_require_admin_allows_admin():
    tg_admins.add_admin(101)
    called = []

    @bot._require_admin
    async def _inner(update, ctx):
        called.append(True)

    msg = _Msg(_User(101), _Chat(101), "/status")
    _run(_inner(_Update(msg), None))
    assert called == [True]
