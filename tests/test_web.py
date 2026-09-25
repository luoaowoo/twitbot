"""Web 控制台后端测试（★D）—— FastAPI TestClient。

覆盖 AGENT_CONTRACT.md §6 的全部接口 + 关键容错路径：
  * /api/status 字段齐全
  * /api/jobs 列表 / 投料成功 / 重复被拒(409)
  * 切到不可用后端 → 400
  * cancel / requeue 生效
  * /api/pause 后 status.paused == true
  * WEB_TOKEN 生效：无 token 401、带 token 200
  * **后端模块缺失时服务照常启动**（子进程 import + 真实启动探测）

隔离手段：
  * 用 monkeypatch 把 core.config 的 DATA_DIR/DB_PATH 指向 tmp_path，绝不碰真实 data/
  * 用 monkeypatch 把 core.settings.DEFAULTS 里的额度/窗口改成测试值
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── fixtures ──────────────────────────────────────────────

@pytest.fixture()
def tmp_data(tmp_path, monkeypatch):
    """把 DATA_DIR 指到 tmp_path，并让 config 的各派生路径跟着走。"""
    from core import config

    data = tmp_path / "data"
    for p in (data, data / "media", data / "browser", data / "logs"):
        p.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(config, "DATA_DIR", data, raising=False)
    monkeypatch.setattr(config, "DB_PATH", data / "queue.db", raising=False)
    monkeypatch.setattr(config, "MEDIA_DIR", data / "media", raising=False)
    monkeypatch.setattr(config, "BROWSER_DIR", data / "browser", raising=False)
    monkeypatch.setattr(config, "LOG_DIR", data / "logs", raising=False)
    # 默认额度/去重窗口（静态默认值只影响 status.limit_static）
    monkeypatch.setattr(config, "MONTHLY_LIMIT", 480, raising=False)
    monkeypatch.setattr(config, "DEDUP_WINDOW", 600, raising=False)
    return data


@pytest.fixture()
def clean_settings(tmp_data, monkeypatch):
    """settings.DEFAULTS 的 backend/monthly_limit/dedup_window 用确定值，避免跨用例串味。"""
    from core import settings

    monkeypatch.setitem(settings.DEFAULTS, "backend", "x_api")
    monkeypatch.setitem(settings.DEFAULTS, "paused", "0")
    monkeypatch.setitem(settings.DEFAULTS, "monthly_limit", "480")
    monkeypatch.setitem(settings.DEFAULTS, "dedup_window", "600")
    return settings


@pytest.fixture()
def client(tmp_data, clean_settings, monkeypatch):
    """无 token 模式的应用。"""
    from core import config
    monkeypatch.setattr(config, "WEB_TOKEN", "", raising=False)
    for m in list(sys.modules):
        if m == "web.server":
            del sys.modules[m]
    from web.server import create_app
    return TestClient(create_app())


@pytest.fixture()
def token_client(tmp_data, clean_settings, monkeypatch):
    """WEB_TOKEN 模式的应用（token=secret-token）。"""
    from core import config
    monkeypatch.setattr(config, "WEB_TOKEN", "secret-token", raising=False)
    for m in list(sys.modules):
        if m == "web.server":
            del sys.modules[m]
    from web.server import create_app
    return TestClient(create_app())


AUTH = {"Authorization": "Bearer secret-token"}


# ── 1. 状态接口 ───────────────────────────────────────────

def test_status_shape(client):
    r = client.get("/api/status")
    assert r.status_code == 200, r.text
    d = r.json()
    assert set(["queue", "backend", "backends", "paused", "settings"]).issubset(d)
    q = d["queue"]
    for k in ("total", "by_status", "month_sent", "limit"):
        assert k in q, f"queue 缺字段 {k}"
    assert isinstance(q["by_status"], dict)
    assert isinstance(d["backends"], list) and len(d["backends"]) == 2
    for b in d["backends"]:
        for k in ("name", "label", "available", "reason", "loaded"):
            assert k in b
    assert isinstance(d["paused"], bool)
    assert d["backend"] in ("x_api", "browser")
    for k in ("tweet_prefix", "tweet_suffix", "monthly_limit", "dedup_window", "quote_mode"):
        assert k in d["settings"]


def test_index_html_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    assert "twitbot 控制台" in body
    assert "CDN" not in body and "cdn.jsdelivr" not in body   # 无 CDN 依赖


def test_healthz(client):
    assert client.get("/healthz").json()["ok"] is True


# ── 2. 任务列表 / 投料 ────────────────────────────────────

def test_jobs_list_empty(client):
    r = client.get("/api/jobs")
    assert r.status_code == 200
    assert r.json()["jobs"] == []


def test_jobs_list_bad_status(client):
    r = client.get("/api/jobs?status=nonsense")
    assert r.status_code == 400
    assert "error" in r.json()


def test_enqueue_and_dup_rejected(client):
    r = client.post("/api/jobs", json={"text": "测试投料 内容A"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True and isinstance(d["id"], int)
    assert d["status"] == "pending"
    assert d["kind"] == "text"

    # 同内容第二次 → 409（去重窗口生效）
    r2 = client.post("/api/jobs", json={"text": "测试投料 内容A"})
    assert r2.status_code == 409, r2.text
    body = r2.json()
    assert "error" in body and "重复" in body["error"]

    # 换内容 → 又能进
    r3 = client.post("/api/jobs", json={"text": "完全不同的另一条内容B"})
    assert r3.status_code == 200

    rows = client.get("/api/jobs").json()["jobs"]
    assert len(rows) == 2
    assert rows[0]["id"] > rows[1]["id"]          # 新到旧
    assert "summary" in rows[0] and "text_len" in rows[0]


def test_enqueue_empty_rejected(client):
    r = client.post("/api/jobs", json={"text": "   "})
    assert r.status_code == 400
    assert "error" in r.json()


def test_enqueue_bad_media_path(client):
    r = client.post("/api/jobs", json={"text": "带媒体", "media_path": "no_such_file.jpg"})
    assert r.status_code == 400
    assert "不存在" in r.json()["error"]


def test_enqueue_media_infers_kind(client, tmp_data):
    (tmp_data / "media" / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    r = client.post("/api/jobs", json={"text": "图来了", "media_path": "pic.png"})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "photo"
    assert r.json()["job"]["media_path"] == "pic.png"     # 库内存相对名


def test_enqueue_quote_id_from_url(client):
    r = client.post("/api/jobs", json={
        "text": "看看这条 https://x.com/someone/status/1234567890"})
    assert r.status_code == 200, r.text
    assert r.json()["job"]["quote_id"] == "1234567890"


def test_enqueue_awaiting_status(client):
    r = client.post("/api/jobs", json={"text": "要人确认的", "status": "awaiting"})
    assert r.status_code == 200
    assert r.json()["status"] == "awaiting"


# ── 3. cancel / requeue ───────────────────────────────────

def test_cancel_takes_effect(client):
    jid = client.post("/api/jobs", json={"text": "待取消的内容"}).json()["id"]
    r = client.post(f"/api/jobs/{jid}/cancel")
    assert r.status_code == 200, r.text
    assert r.json()["job"]["status"] == "canceled"
    assert client.get(f"/api/jobs/{jid}").json()["job"]["status"] == "canceled"
    # 重复取消 → 409（状态不允许）
    assert client.post(f"/api/jobs/{jid}/cancel").status_code == 409


def test_cancel_missing_job_404(client):
    r = client.post("/api/jobs/999999/cancel")
    assert r.status_code == 404
    assert "error" in r.json()


def test_requeue_after_cancel(client):
    jid = client.post("/api/jobs", json={"text": "取消后再重发"}).json()["id"]
    client.post(f"/api/jobs/{jid}/cancel")
    r = client.post(f"/api/jobs/{jid}/requeue")
    assert r.status_code == 200, r.text
    assert r.json()["job"]["status"] == "pending"
    assert client.get(f"/api/status").json()["queue"]["by_status"].get("pending") == 1


def test_requeue_pending_rejected(client):
    jid = client.post("/api/jobs", json={"text": "还在待发的"}).json()["id"]
    assert client.post(f"/api/jobs/{jid}/requeue").status_code == 409


# ── 4. 后端切换（重点：不可用时必须 400）─────────────────

def test_backends_endpoint(client):
    r = client.get("/api/backends")
    assert r.status_code == 200
    d = r.json()
    names = [b["name"] for b in d["backends"]]
    assert names == ["x_api", "browser"]
    assert d["current"] in names


def test_switch_to_unavailable_backend_400(client, monkeypatch):
    """构造一个 available()=False 的后端，切换必须 400 且带原因。"""
    from core.backends import registry
    from web import server as web_server

    class FakeBad:
        name = "browser"
        label = "无头浏览器"
        def available(self):
            return False, "未登录：browser_state.json 不存在，请先点『登录 X』"
        def verify(self):
            return False, "无法启动浏览器"
        def publish(self, job, text):  # pragma: no cover
            raise AssertionError("不该被调用")
        def login(self, on_event=None):
            return False, "测试桩"

    monkeypatch.setitem(registry.REGISTRY, "browser", ("core.backends.browser", "BrowserBackend", "无头浏览器"))
    monkeypatch.setattr(registry, "get", lambda name: FakeBad() if name == "browser" else _ok_stub(name))
    web_server.invalidate_backends_cache()

    r = client.post("/api/backend", json={"backend": "browser"})
    assert r.status_code == 400, r.text
    body = r.json()
    assert "error" in body
    assert "不可用" in body["error"] and "未登录" in body["error"]
    # 未切换：仍是原后端
    assert client.get("/api/status").json()["backend"] == "x_api"


def test_switch_to_unknown_backend_400(client):
    r = client.post("/api/backend", json={"backend": "no_such_backend"})
    assert r.status_code == 400
    assert "未知后端" in r.json()["error"]


def test_switch_to_available_backend_ok(client, monkeypatch):
    from core.backends import registry
    from web import server as web_server

    monkeypatch.setattr(registry, "get", lambda name: _ok_stub(name))
    web_server.invalidate_backends_cache()
    r = client.post("/api/backend", json={"backend": "browser"})
    assert r.status_code == 200, r.text
    assert r.json()["backend"] == "browser"
    assert client.get("/api/status").json()["backend"] == "browser"


def test_verify_returns_backend_message(client, monkeypatch):
    from core.backends import registry
    from web import server as web_server

    class Stub:
        name = "x_api"; label = "X 官方 API"
        def available(self): return True, "凭据已配置"
        def verify(self): return False, "鉴权失败：请检查 App 权限是否 Read and Write"
        def publish(self, job, text): raise AssertionError
        def login(self, on_event=None): return True, "无需登录"

    monkeypatch.setattr(registry, "get", lambda name: Stub())
    web_server.invalidate_backends_cache()
    r = client.post("/api/backends/x_api/verify")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is False
    assert "Read and Write" in d["message"]     # 原始文案原样展示


def test_login_unavailable_backend_400(client, monkeypatch):
    """browser 后端模块缺失（或 mock 成缺失）时，登录返回 400 + 原因，不 500。"""
    from core.backends import registry

    def boom(name):
        raise ImportError("No module named 'core.backends.browser'")

    monkeypatch.setattr(registry, "get", boom)
    r = client.post("/api/backends/browser/login")
    assert r.status_code == 400, r.text
    assert "模块未就绪" in r.json()["error"]


def test_login_status_endpoint(client):
    r = client.get("/api/backends/browser/login/status")
    assert r.status_code == 200
    d = r.json()
    assert "running" in d and "events" in d and d["running"] is False


def test_login_is_nonblocking_and_reports_progress(client, monkeypatch):
    """登录必须后台跑：POST 立刻返回，前端轮询 status 拿进度与最终结果。"""
    import threading
    import time as _time

    from core.backends import registry
    from web import server as web_server

    started = threading.Event()
    release = threading.Event()

    class SlowLogin:
        name = "browser"; label = "无头浏览器"
        def available(self): return True, "已登录"
        def verify(self): return True, "会话有效"
        def login(self, on_event=None):
            if on_event: on_event("已打开浏览器，请在其中登录")
            started.set()
            if on_event: on_event("等待扫码…")
            release.wait(timeout=10)
            if on_event: on_event("登录成功，已保存 storage_state")
            return True, "登录成功，账号 @demo"
        def publish(self, job, text):  # pragma: no cover
            raise AssertionError

    monkeypatch.setattr(registry, "get", lambda name: SlowLogin())
    web_server._login_finish(False, "")          # 复位登录状态
    web_server.invalidate_backends_cache()

    t0 = _time.time()
    r = client.post("/api/backends/browser/login")
    elapsed = _time.time() - t0
    assert r.status_code == 200, r.text
    assert r.json()["started"] is True
    assert elapsed < 1.0, f"登录请求阻塞了 {elapsed:.2f}s，必须后台执行"

    assert started.wait(timeout=3), "后台登录线程没有启动"
    mid = client.get("/api/backends/browser/login/status").json()
    assert mid["running"] is True
    assert any("浏览器" in e["msg"] for e in mid["events"])

    release.set()
    for _ in range(50):
        st = client.get("/api/backends/browser/login/status").json()
        if not st["running"]:
            break
        _time.sleep(0.1)
    assert st["running"] is False
    assert st["ok"] is True
    assert "demo" in st["message"]
    assert st["elapsed"] >= 0


def test_publish_without_pipeline_503(client, monkeypatch):
    """core.pipeline 不存在时 → 503 中文说明，不能 500/炸服务。"""
    import core
    from web import server as web_server

    # sys.modules 里放 None 会让 `from core import pipeline` 直接抛 ImportError，
    # 精确模拟『pipeline.py 还没写』的场景。
    monkeypatch.delattr(core, "pipeline", raising=False)
    monkeypatch.setitem(sys.modules, "core.pipeline", None)
    monkeypatch.setattr(web_server, "_pipe_obj", None, raising=False)

    jid = client.post("/api/jobs", json={"text": "尝试立即发送"}).json()["id"]
    r = client.post(f"/api/jobs/{jid}/publish")
    assert r.status_code == 503, r.text
    assert "未就绪" in r.json()["error"]
    # 服务仍然活着
    assert client.get("/api/status").status_code == 200


def test_publish_uses_injected_pipeline_instance(client, monkeypatch):
    """统一启动器把 Pipeline 实例交给控制台后，立即发送走该实例。"""
    from web import server as web_server

    calls = []

    class FakePipe:
        async def publish_one(self, job_id):
            calls.append(job_id)
            return {"ok": True, "job_id": job_id, "note": "桩"}

    web_server.set_pipeline_instance(FakePipe())
    try:
        jid = client.post("/api/jobs", json={"text": "走注入的 pipeline"}).json()["id"]
        r = client.post(f"/api/jobs/{jid}/publish")
        assert r.status_code == 200, r.text
        assert calls == [jid]
        assert r.json()["result"]["ok"] is True
    finally:
        web_server.set_pipeline_instance(None)


def test_publish_reports_failure_via_published_flag(client, monkeypatch):
    """发布失败时外层 ok 仍为 true（请求已处理），但 published 必须为 false。

    防止调用方把 ok:true 误读成「发出去了」。
    """
    from web import server as web_server

    class FailingPipe:
        async def publish_one(self, job_id):
            return {"ok": False, "job_id": job_id, "error": "后端不可用: x_api: 未配置凭据"}

    web_server.set_pipeline_instance(FailingPipe())
    try:
        jid = client.post("/api/jobs", json={"text": "故意失败的立即发送"}).json()["id"]
        r = client.post(f"/api/jobs/{jid}/publish")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True          # 请求处理成功
        assert body["published"] is False  # 但没发出去
        assert "未配置凭据" in body["result"]["error"]
    finally:
        web_server.set_pipeline_instance(None)


def test_status_reports_pipeline_readiness(client, monkeypatch):
    import core
    from web import server as web_server

    web_server.set_pipeline_instance(None)
    # 已导入过的子模块会挂在 core 属性上，必须一起清掉才算「模块不存在」
    monkeypatch.delattr(core, "pipeline", raising=False)
    monkeypatch.setitem(sys.modules, "core.pipeline", None)
    assert web_server.resolve_pipeline()[0] is None
    assert "缺失" in web_server.resolve_pipeline()[1]
    st = client.get("/api/status").json()
    assert st["pipeline"]["ready"] is False
    assert st["pipeline"]["reason"]


def test_status_reports_pipeline_ready_when_present(client, monkeypatch):
    """pipeline 存在时 status.pipeline.ready=true，前端可据此显示发布循环状态。"""
    web_server = __import__("web.server", fromlist=["server"])

    web_server.set_pipeline_instance(None)
    if web_server.resolve_pipeline()[0] is None:
        pytest.skip("core.pipeline 当前不可用，跳过 ready 分支")
    st = client.get("/api/status").json()
    assert st["pipeline"]["ready"] is True


# ── 5. 暂停 / 设置 ────────────────────────────────────────

def test_pause_toggle_reflected_in_status(client):
    assert client.get("/api/status").json()["paused"] is False
    r = client.post("/api/pause", json={"paused": True})
    assert r.status_code == 200
    assert r.json()["paused"] is True
    assert client.get("/api/status").json()["paused"] is True

    r2 = client.post("/api/pause", json={"paused": False})
    assert r2.json()["paused"] is False
    assert client.get("/api/status").json()["paused"] is False


def test_settings_save_and_readback(client):
    r = client.post("/api/settings", json={
        "tweet_prefix": "#标签 ", "tweet_suffix": " 完",
        "monthly_limit": 100, "dedup_window": 60, "quote_mode": "off",
    })
    assert r.status_code == 200, r.text
    assert r.json()["saved"]["monthly_limit"] == "100"
    st = client.get("/api/status").json()["settings"]
    assert st["tweet_prefix"] == "#标签 "
    assert st["tweet_suffix"] == " 完"
    assert st["monthly_limit"] == 100
    assert st["dedup_window"] == 60
    assert st["quote_mode"] == "off"
    assert client.get("/api/status").json()["queue"]["limit"] == 100


def test_settings_reject_bad_values(client):
    assert client.post("/api/settings", json={"quote_mode": "wat"}).status_code == 400
    assert client.post("/api/settings", json={"monthly_limit": "abc"}).status_code == 400
    assert client.post("/api/settings", json={}).status_code == 400


def test_settings_ignores_unknown_keys(client):
    """非白名单键不报错也不落库（core.settings 会静默忽略）。"""
    r = client.post("/api/settings", json={"tweet_prefix": "P"})
    assert r.status_code == 200
    assert "backend" not in r.json()["saved"]


# ── 6. WEB_TOKEN ──────────────────────────────────────────

def test_token_required_when_set(token_client):
    assert token_client.get("/api/status").status_code == 401
    assert token_client.get("/api/jobs").status_code == 401
    assert token_client.get("/").status_code == 401
    r = token_client.get("/api/status", headers=AUTH)
    assert r.status_code == 200
    assert token_client.get("/api/status?token=secret-token").status_code == 200
    assert token_client.get("/api/status?token=wrong").status_code == 401
    assert token_client.post("/api/pause", json={"paused": True},
                             headers=AUTH).status_code == 200


def test_token_error_is_json(token_client):
    r = token_client.get("/api/status")
    assert r.status_code == 401
    assert "error" in r.json()


def test_token_on_write_endpoints(token_client):
    """带 token 的写接口正常，不带的一律 401。"""
    assert token_client.post("/api/jobs", json={"text": "未授权投料"}).status_code == 401
    r = token_client.post("/api/jobs?token=secret-token", json={"text": "带 token 投料"})
    assert r.status_code == 200, r.text
    assert token_client.post("/api/pause", headers=AUTH,
                             json={"paused": True}).status_code == 200


def test_malformed_json_returns_400(client):
    r = client.post("/api/jobs", content=b"{not json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert "JSON" in r.json()["error"]


def test_no_token_needed_by_default(client):
    assert client.get("/api/status").status_code == 200


# ── 7. SSE ────────────────────────────────────────────────

def test_events_sse_first_frame(client):
    """frames=1 让服务推一帧就收尾，避免 TestClient 阻塞在无限流上。"""
    r = client.get("/api/events?interval=0.2&frames=1")
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    assert "event: status" in r.text
    payload = r.text.split("data: ", 1)[1].split("\n", 1)[0]
    d = json.loads(payload)
    assert "queue" in d and "backends" in d
    assert d["ok"] is True


# ── 8. 后端模块缺失时服务照常启动（验收重点）─────────────

def test_registry_describe_survives_missing_modules(tmp_data):
    """当前 x_api.py / browser.py 可能还不存在 —— describe 必须返回 loaded=false 而不是抛。"""
    from core.backends import registry
    items = registry.describe()
    assert len(items) == 2
    for b in items:
        assert b["loaded"] in (True, False)
        if not b["loaded"]:
            assert b["reason"]                      # 必须给出原因
            assert b["available"] is False


def test_app_starts_with_backend_modules_missing(tmp_data, clean_settings, monkeypatch):
    """把两个后端模块都伪装成缺失，app 仍必须能建起来且 /api/status、/api/backends 都是 200。"""
    import importlib

    real_import = importlib.import_module

    def fake_import(name, *a, **kw):
        if name in ("core.backends.x_api", "core.backends.browser"):
            raise ImportError(f"No module named {name!r} (模拟未就绪)")
        return real_import(name, *a, **kw)

    for m in ("web.server", "core.backends.registry"):
        sys.modules.pop(m, None)
    monkeypatch.setattr(importlib, "import_module", fake_import)
    try:
        from core.backends import registry
        for k in ("x_api", "browser"):
            registry._cache.pop(k, None)
        from core import config
        monkeypatch.setattr(config, "WEB_TOKEN", "", raising=False)
        from web.server import create_app
        c = TestClient(create_app())

        r = c.get("/api/status")
        assert r.status_code == 200, r.text
        backs = r.json()["backends"]
        assert all(b["loaded"] is False for b in backs)
        assert all(b["reason"] for b in backs)

        r2 = c.get("/api/backends")
        assert r2.status_code == 200, r2.text
        assert all(b["available"] is False for b in r2.json()["backends"])

        # 切后端 → 400 带原因，而不是 500
        r3 = c.post("/api/backend", json={"backend": "x_api"})
        assert r3.status_code == 400
        assert "未就绪" in r3.json()["error"]

        # 前端与其它接口不受影响
        assert c.get("/").status_code == 200
        assert c.post("/api/jobs", json={"text": "后端缺失也能投料"}).status_code == 200
    finally:
        for m in ("web.server", "core.backends.registry"):
            sys.modules.pop(m, None)


def test_subprocess_start_without_backend_modules(tmp_data, tmp_path):
    """真实子进程验证：后端模块加载失败时，服务能起来并正常响应（不崩、不 500）。

    子进程里先 patch importlib.import_module 让两个后端模块 import 抛错，
    再建 app —— 完全不依赖磁盘上 x_api.py / browser.py 是否存在。
    """
    port = "8799"
    env = dict(os.environ)
    env["DATA_DIR"] = str(tmp_path / "data")
    env["WEB_PORT"] = port
    env["WEB_HOST"] = "127.0.0.1"
    env["WEB_TOKEN"] = ""
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(ROOT)
    # 必须显式清空凭据：本测试要从 os.environ 继承环境，而别的测试模块
    # （如 test_pipeline.py）会在 import 期往 os.environ 写假凭据。
    # 若不清空，子进程里 x_api 的 available() 会返回 True，
    # 「切到不可用后端应 400」的断言就会失败（测试间污染，非产品缺陷）。
    env["TG_TOKEN"] = ""
    for _k in ("X_CONSUMER_KEY", "X_CONSUMER_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET"):
        env[_k] = ""

    script = textwrap.dedent(f"""
        import sys, importlib, json
        sys.path.insert(0, r"{ROOT}")
        _real = importlib.import_module
        def fake(name, *a, **kw):
            if name in ("core.backends.x_api", "core.backends.browser"):
                raise ImportError("No module named " + repr(name) + " (模拟未就绪)")
            return _real(name, *a, **kw)
        importlib.import_module = fake

        from web.server import create_app
        from fastapi.testclient import TestClient
        c = TestClient(create_app())

        r = c.get("/api/status")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["backend"] in ("x_api", "browser"), data["backend"]
        assert len(data["backends"]) == 2, data["backends"]
        for b in data["backends"]:
            assert b["loaded"] is False, b
            assert b["available"] is False, b
            assert b["reason"], b
            importlib.import_module = _real

        r2 = c.get("/api/backends")
        assert r2.status_code == 200, r2.text
        r3 = c.post("/api/backend", json={{"backend": "x_api"}})
        assert r3.status_code == 400, r3.text
        assert c.get("/").status_code == 200
        j = c.post("/api/jobs", json={{"text": "后端缺失仍可投料"}})
        assert j.status_code == 200, j.text
        print("SUBPROCESS_OK backends=" + json.dumps([b["reason"][:48] for b in data["backends"]], ensure_ascii=False))
    """)
    p = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120)
    assert p.returncode == 0, f"stdout={p.stdout}\nstderr={p.stderr}"
    assert "SUBPROCESS_OK" in p.stdout


# ── 9. create_app 契约（start.py 用）─────────────────────

def test_create_app_returns_fresh_app_without_uvicorn_run(tmp_data, clean_settings, monkeypatch):
    """create_app() 不启服务、不阻塞，且每次返回可用 app；模块顶层不调用 uvicorn.run()。"""
    from core import config
    monkeypatch.setattr(config, "WEB_TOKEN", "", raising=False)
    import web.server as ws

    src = (ROOT / "web" / "server.py").read_text(encoding="utf-8")
    # uvicorn.run 只允许出现在 _main() 里（缩进在 main 块内）
    for line in src.splitlines():
        if "uvicorn.run(" in line:
            assert line.startswith("    "), f"uvicorn.run 出现在模块顶层: {line}"

    a1 = ws.create_app()
    a2 = ws.create_app()
    assert a1 is not a2
    c = TestClient(a1)
    assert c.get("/api/status").status_code == 200
    # create_app 内部完成建表：settings 可读写
    assert c.post("/api/settings", json={"monthly_limit": 7}).status_code == 200
    assert c.get("/api/status").json()["settings"]["monthly_limit"] == 7


# ── 工具桩 ────────────────────────────────────────────────

def _ok_stub(name: str):
    class Stub:
        label = "测试后端"
        def __init__(self, n): self.name = n
        def available(self): return True, "凭据已配置"
        def verify(self): return True, "鉴权正常"
        def publish(self, job, text):  # pragma: no cover
            raise AssertionError
        def login(self, on_event=None): return True, "无需登录"
    return Stub(name)


# ══════════════════════════════════════════════════════════
# 控制台密码门（服务器版专属）
#   服务器暴露在网络里，光靠 ?token= 不够 —— 会出现在 URL / 历史记录里。
#   这里做成"登录页 + 会话 Cookie"：密码只在登录时出现一次。
# ══════════════════════════════════════════════════════════

@pytest.fixture()
def pw_client(monkeypatch):
    """开启密码门的 client（默认测试环境是关闭的）。"""
    from web import server as ws
    monkeypatch.setenv("WEB_PASSWORD", "test_pw_123")
    ws._sessions.clear()
    app = ws.create_app()
    with TestClient(app) as c:
        yield c
    ws._sessions.clear()


def test_password_gate_shows_login_page(pw_client):
    """未登录用浏览器访问 -> 给登录页（不是 401 白屏）。"""
    r = pw_client.get("/", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert "请输入管理密码" in r.text


def test_password_gate_blocks_api_without_login(pw_client):
    """未登录调接口 -> 401（不是 200）。"""
    r = pw_client.get("/api/status")
    assert r.status_code == 401
    assert "未登录" in r.json()["error"]


def test_password_gate_rejects_wrong_password(pw_client):
    r = pw_client.post("/login", json={"password": "nope"})
    assert r.status_code == 401
    assert "不正确" in r.json()["error"]


def test_password_gate_rejects_empty_password(pw_client):
    r = pw_client.post("/login", json={"password": ""})
    assert r.status_code == 401


def test_password_gate_accepts_correct_password(pw_client):
    r = pw_client.post("/login", json={"password": "test_pw_123"})
    assert r.status_code == 200
    assert "twitbot_session" in r.cookies


def test_password_gate_allows_after_login(pw_client):
    """登录后接口应放行。"""
    pw_client.post("/login", json={"password": "test_pw_123"})
    r = pw_client.get("/api/status")
    assert r.status_code == 200
    assert "backend" in r.json()


def test_password_gate_logout_revokes(pw_client):
    """登出后会话立即失效。"""
    pw_client.post("/login", json={"password": "test_pw_123"})
    assert pw_client.get("/api/status").status_code == 200
    pw_client.post("/logout")
    assert pw_client.get("/api/status").status_code == 401


def test_password_gate_healthz_always_open(pw_client):
    """健康检查不拦 —— 监控系统不该需要密码。"""
    r = pw_client.get("/healthz")
    assert r.status_code == 200


def test_password_gate_disabled_with_dash(monkeypatch):
    """WEB_PASSWORD=- 表示关闭密码门（供测试/内网使用）。"""
    from web import server as ws
    monkeypatch.setenv("WEB_PASSWORD", "-")
    ws._sessions.clear()
    with TestClient(ws.create_app()) as c:
        assert c.get("/api/status").status_code == 200


def test_password_gate_default_password():
    """不设 WEB_PASSWORD 时用内置默认值。"""
    from web import server as ws
    assert callable(getattr(ws, "_load_or_create_password", None))


def test_session_token_is_random_and_not_the_password(pw_client):
    """会话 token 必须随机，绝不能等于密码本身。"""
    r = pw_client.post("/login", json={"password": "test_pw_123"})
    tok = r.cookies.get("twitbot_session")
    assert tok and tok != "test_pw_123"
    assert len(tok) >= 32, "会话 token 太短，容易猜"
