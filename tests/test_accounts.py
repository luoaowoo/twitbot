"""控制台多账号登录与数据隔离回归测试。"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _reset_server_module() -> None:
    for name in list(sys.modules):
        if name == "web.server":
            del sys.modules[name]


def test_console_login_account_isolation(tmp_path, monkeypatch):
    from core import accounts

    monkeypatch.delenv("TWITBOT_AUTH_DISABLED", raising=False)
    monkeypatch.setattr(accounts, "_base_data_dir", tmp_path / "data", raising=False)
    monkeypatch.setattr(accounts, "_session_secret", None, raising=False)
    monkeypatch.setattr(accounts, "auth_enabled", lambda: True)
    _reset_server_module()
    from web.server import create_app

    with TestClient(create_app()) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/status").status_code == 401

        bad = client.post("/api/auth/login", json={
            "username": "qwqcon", "password": "bad-password"})
        assert bad.status_code == 401

        ok = client.post("/api/auth/login", json={
            "username": "qwqcon", "password": "qwqcon_qwqcon"})
        assert ok.status_code == 200, ok.text
        assert ok.json()["username"] == "qwqcon"

        created = client.post("/api/jobs", json={"text": "qwqcon 的独立内容"})
        assert created.status_code == 200, created.text
        assert len(client.get("/api/jobs").json()["jobs"]) == 1

        client.post("/api/auth/logout")
        assert client.get("/api/jobs").status_code == 401

        ok = client.post("/api/auth/login", json={
            "username": "luoaowoo", "password": "luoaowoo_luoaowoo"})
        assert ok.status_code == 200, ok.text
        assert client.get("/api/jobs").json()["jobs"] == []
        created = client.post("/api/jobs", json={"text": "luoaowoo 的独立内容"})
        assert created.status_code == 200, created.text
        assert len(client.get("/api/jobs").json()["jobs"]) == 1


def test_password_rules_and_signed_session():
    from core import accounts

    assert accounts.verify_password("qwqcon", "qwqcon_qwqcon")
    assert accounts.verify_password("luoaowoo", "luoaowoo_luoaowoo")
    assert not accounts.verify_password("qwqcon", "qwqcon_用户米那个")
    assert not accounts.verify_password("qwqcon", "wrong")

    token = accounts.issue_session("qwqcon")
    assert accounts.verify_session(token) == "qwqcon"
    assert accounts.verify_session(token + "x") is None
    assert accounts.verify_session("") is None


def test_account_paths_are_independent(tmp_path, monkeypatch):
    from core import accounts, queue, settings
    from core.backends.browser import BrowserBackend

    monkeypatch.setattr(accounts, "_base_data_dir", tmp_path / "data", raising=False)
    accounts.init_accounts()

    with accounts.use_account("qwqcon"):
        assert accounts.account_dir() == tmp_path / "data" / "accounts" / "qwqcon"
        assert Path(str(queue.db().execute("PRAGMA database_list").fetchone()[2])) == (
            tmp_path / "data" / "accounts" / "qwqcon" / "queue.db")
        assert BrowserBackend().state_path() == (
            tmp_path / "data" / "accounts" / "qwqcon" / "browser" / "storage_state.json")
        settings.set_many({"tweet_prefix": "A:"})

    with accounts.use_account("luoaowoo"):
        assert accounts.account_dir() == tmp_path / "data" / "accounts" / "luoaowoo"
        assert Path(str(queue.db().execute("PRAGMA database_list").fetchone()[2])) == (
            tmp_path / "data" / "accounts" / "luoaowoo" / "queue.db")
        assert BrowserBackend().state_path() == (
            tmp_path / "data" / "accounts" / "luoaowoo" / "browser" / "storage_state.json")
        assert settings.get("tweet_prefix") != "A:"

    with accounts.use_account("qwqcon"):
        assert settings.get("tweet_prefix") == "A:"
