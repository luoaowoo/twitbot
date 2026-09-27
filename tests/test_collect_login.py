"""扫码登录接口的回归护栏：`force` 必须透传到采集器。

2026-09-27 的真实 bug：前端「重新生成二维码」带了 force=true，
但 /api/collect/login/start 的路由把它丢了，导致旧登录线程继续覆盖新二维码。
这个测试钉住「路由收到 force 后必须原样调用 collector.login_start(force=...)」。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from core import collect as collect_mod


class _FakeCollector:
    def __init__(self) -> None:
        self.calls: list[bool] = []

    def login_start(self, force: bool = False):
        self.calls.append(bool(force))
        return True, "准备中", "data:image/png;base64,xx"

    def login_status(self):
        return {"running": True, "ok": None, "message": "准备中", "qr": "",
                "logged_in": False, "account": ""}


def _client(monkeypatch, fake: _FakeCollector) -> TestClient:
    monkeypatch.setattr(
        collect_mod, "get_collector",
        lambda name: fake if name == "xiaohongshu" else None)
    from web.server import create_app
    return TestClient(create_app())


def test_collect_login_forwards_force(monkeypatch):
    fake = _FakeCollector()
    client = _client(monkeypatch, fake)
    r = client.post("/api/collect/login/start",
                    json={"platform": "xiaohongshu", "force": True})
    assert r.status_code == 200, r.text
    assert fake.calls == [True]


def test_collect_login_defaults_to_no_force(monkeypatch):
    fake = _FakeCollector()
    client = _client(monkeypatch, fake)
    r = client.post("/api/collect/login/start",
                    json={"platform": "xiaohongshu"})
    assert r.status_code == 200, r.text
    assert fake.calls == [False]
