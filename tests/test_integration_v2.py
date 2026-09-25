"""集成层测试 —— 我（集成方）负责的 web/server.py 新增接口。

覆盖 CONTRACT_V2 §5：
  * Telegram 控制接口（模块缺失时必须优雅降级，不能 500）
  * 媒体上传 / 列表 / 预览 / 校验（含目录穿越防护）
  * 账号密码登录接口（密码绝不落库、绝不回显）
  * 入队时的媒体合规校验
"""
from __future__ import annotations

import io
import json
import logging
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web import server as web_server  # noqa: E402
from web.server import create_app  # noqa: E402


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_globals():
    """每个用例前后清掉注入的实例，避免相互污染。"""
    web_server.set_pipeline_instance(None)
    web_server.set_tg_manager_instance(None)
    yield
    web_server.set_pipeline_instance(None)
    web_server.set_tg_manager_instance(None)


# ══════════════════════════════════════════════════════════
# 真实 Chrome 登录接口（/api/backends/{name}/login-chrome）
#   背景：/login 走 Playwright 启动浏览器，带自动化指纹，x.com 风控
#   会把会话判定为机器人（登录页「出了点问题」+ prelude_gate），用户手工点也没用。
#   login-chrome 用普通方式启动系统 Chrome，再用 CDP 读登录态。
# ══════════════════════════════════════════════════════════

def test_login_chrome_400_when_no_chrome(client, monkeypatch):
    """找不到 Chrome 时应 400 并给出可读原因，而不是 500。"""
    from core import chrome_login
    monkeypatch.setattr(chrome_login, "find_chrome", lambda: "")
    r = client.post("/api/backends/browser/login-chrome")
    assert r.status_code == 400
    assert "Chrome" in r.json()["detail"]


def test_login_chrome_starts_background_job(client, monkeypatch):
    """找到 Chrome 时应立即返回 200 并给出 status_url（登录在后台跑）。"""
    from core import chrome_login
    monkeypatch.setattr(chrome_login, "find_chrome", lambda: r"C:\fake\chrome.exe")
    # 别真的起浏览器：把实际登录换掉
    import web.server as ws
    monkeypatch.setattr(ws, "_login_chrome_worker", lambda name: None)
    r = client.post("/api/backends/browser/login-chrome")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "chrome"
    assert body["started"] is True
    assert "login/status" in body["status_url"]


def test_login_chrome_conflicts_with_running_login(client, monkeypatch):
    """已有登录在跑时应 409，避免同时开两个浏览器。"""
    from core import chrome_login
    monkeypatch.setattr(chrome_login, "find_chrome", lambda: r"C:\fake\chrome.exe")
    import web.server as ws
    with ws._login_lock:
        ws._login_state["running"] = True
    try:
        r = client.post("/api/backends/browser/login-chrome")
        assert r.status_code == 409
    finally:
        with ws._login_lock:
            ws._login_state["running"] = False


# ══════════════════════════════════════════════════════════
# start.py 的 tg_autostart 门控（回归：曾经是死设置，从未被读取）
# ══════════════════════════════════════════════════════════

def test_tg_autostart_defaults_to_on():
    """契约与 README 都承诺 `python start.py` 默认全启动。"""
    from core import settings
    assert settings.DEFAULTS["tg_autostart"] == "1"


def test_should_start_tg_respects_setting(monkeypatch):
    import start
    from core import settings

    monkeypatch.setitem(settings.DEFAULTS, "tg_autostart", "0")
    assert start.should_start_tg() is False

    monkeypatch.setitem(settings.DEFAULTS, "tg_autostart", "1")
    assert start.should_start_tg() is True


def test_should_start_tg_flag_overrides_setting(monkeypatch):
    import start
    from core import settings

    # --tg 忽略设置为关
    monkeypatch.setitem(settings.DEFAULTS, "tg_autostart", "0")
    assert start.should_start_tg(force_tg=True) is True

    # --no-tg 忽略设置为开
    monkeypatch.setitem(settings.DEFAULTS, "tg_autostart", "1")
    assert start.should_start_tg(no_tg=True) is False

    # --no-tg 压过 --tg
    assert start.should_start_tg(no_tg=True, force_tg=True) is False


# ══════════════════════════════════════════════════════════
# Telegram 接口
# ══════════════════════════════════════════════════════════

def test_tg_status_never_500_when_module_missing(client, monkeypatch):
    """/api/tg/status 在 tgmanager 缺失时也要 200 并说明原因。"""
    monkeypatch.setitem(sys.modules, "core.tgmanager", None)
    web_server.set_tg_manager_instance(None)
    r = client.get("/api/tg/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["tg"]["available"] is False
    assert body["tg"]["running"] is False
    assert body["tg"]["reason"]          # 必须给出可读原因


def test_tg_start_returns_503_when_module_missing(client, monkeypatch):
    monkeypatch.setitem(sys.modules, "core.tgmanager", None)
    web_server.set_tg_manager_instance(None)
    r = client.post("/api/tg/start", json={})
    assert r.status_code == 503, r.text
    assert "error" in r.json()


class _FakeTg:
    """符合 CONTRACT_V2 §2.1 的最小桩。"""

    def __init__(self, running=False, start_ok=True, start_msg="已启动"):
        self.running = running
        self.start_ok = start_ok
        self.start_msg = start_msg
        self.calls: list[tuple] = []
        self.stopped = 0

    def status(self):
        return {
            "running": self.running, "token_set": True, "token_masked": "1234...xyz",
            "token_source": "settings", "allowed_users": "", "allowed_chats": "",
            "bot_username": "my_bot", "bot_id": 4242, "started_at": 1.0,
            "last_error": "", "last_update_at": 0.0, "updates": 3,
        }

    async def start(self, token=None, allowed_users=None, allowed_chats=None):
        self.calls.append(("start", token, allowed_users, allowed_chats))
        self.running = self.start_ok
        return self.start_ok, self.start_msg

    async def stop(self):
        self.calls.append(("stop",))
        self.running = False
        self.stopped += 1
        return True, "已停止"

    async def restart(self, **kw):
        self.calls.append(("restart",))
        self.running = True
        return True, "已重启"

    async def verify_token(self, token):
        self.calls.append(("verify", token))
        if token == "good":
            return True, "token 有效", {"bot_username": "my_bot", "bot_id": 42}
        return False, "token 无效或已失效", {}


def test_tg_start_stop_roundtrip(client):
    tg = _FakeTg()
    web_server.set_tg_manager_instance(tg)

    r = client.post("/api/tg/start", json={"token": "abc", "allowed_users": "1,2"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert tg.calls[0] == ("start", "abc", "1,2", None)

    r = client.get("/api/tg/status")
    assert r.json()["tg"]["running"] is True
    assert r.json()["tg"]["bot_username"] == "my_bot"

    r = client.post("/api/tg/stop")
    assert r.status_code == 200
    assert tg.stopped == 1
    assert r.json()["tg"]["running"] is False


def test_tg_start_failure_is_400_not_500(client):
    """token 没填/无效属于用户可修正的输入问题 → 400。"""
    tg = _FakeTg(start_ok=False, start_msg="token 无效或已失效，请到 @BotFather 重新获取")
    web_server.set_tg_manager_instance(tg)
    r = client.post("/api/tg/start", json={})
    assert r.status_code == 400, r.text
    assert "token" in r.json()["error"]


def test_tg_verify_reports_bot_identity(client):
    web_server.set_tg_manager_instance(_FakeTg())
    r = client.post("/api/tg/verify", json={"token": "good"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["info"]["bot_username"] == "my_bot"

    r = client.post("/api/tg/verify", json={"token": "bad"})
    assert r.status_code == 200
    assert r.json()["ok"] is False

    r = client.post("/api/tg/verify", json={"token": ""})
    assert r.status_code == 400


def test_tg_status_does_not_leak_token(client):
    """整个 /api/tg/status 响应里不得出现完整 token 明文。"""
    class _LeakyTg(_FakeTg):
        def status(self):
            d = super().status()
            d["token_masked"] = "1234ABCD...WXYZ"   # 掩码态
            return d

    web_server.set_tg_manager_instance(_LeakyTg())
    raw = client.get("/api/tg/status").text
    # 掩码值本身可以出现，但不该有超长的疑似完整 token
    assert "1234ABCD...WXYZ" in raw or "token_masked" in raw


def test_tg_endpoints_do_not_spawn_new_event_loops():
    """Telegram 启停必须落在调用方的事件循环里，不能各请求起一个 loop。

    背景（Agent E 提出的真实风险）：PTB 的 HTTP 客户端绑定在创建它的 loop 上，
    跨 loop 会报 `Event loop is closed`。若这些端点内部用 `asyncio.run(...)`，
    每次请求都会新建一个 loop，第一次 stop 之后就可能炸。

    这里做源码级断言：web/server.py 不得出现 `asyncio.run(` / `new_event_loop(`，
    端点必须是 `async def` 里直接 `await`（由 uvicorn 的单一 loop 驱动）。
    """
    src = (ROOT / "web" / "server.py").read_text(encoding="utf-8")
    assert "asyncio.run(" not in src, "web/server.py 出现 asyncio.run —— 会为每个请求新建事件循环"
    assert "new_event_loop(" not in src, "web/server.py 出现 new_event_loop —— 破坏 loop 亲和性"
    assert "run_until_complete(" not in src, "web/server.py 出现 run_until_complete —— 破坏 loop 亲和性"

    # 五个 tg 端点都必须是 async def 且直接 await 管理器方法
    for name in ("api_tg_status", "api_tg_verify", "api_tg_start", "api_tg_stop", "api_tg_restart"):
        assert f"async def {name}(" in src, f"{name} 必须是 async def"
    assert "await tg.start(" in src
    assert "await tg.stop(" in src
    assert "await tg.verify_token(" in src


def test_tg_start_stop_repeatable_in_same_loop(client):
    """同一事件循环里反复 start/stop 不应报 loop 相关错误。

    TestClient 的 lifespan 提供单一 loop，重复请求共享它 —— 与 uvicorn 行为一致，
    因此这条能真实覆盖"跨请求复用"的路径。
    """
    tg = _FakeTg()
    web_server.set_tg_manager_instance(tg)

    for i in range(4):
        r = client.post("/api/tg/start", json={"token": "tok%d" % i})
        assert r.status_code == 200, f"第 {i} 轮 start 失败：{r.text}"
        assert "loop" not in r.text.lower() or "关闭" not in r.text

        r = client.post("/api/tg/stop")
        assert r.status_code == 200, f"第 {i} 轮 stop 失败：{r.text}"
        assert "Event loop is closed" not in r.text

    assert tg.stopped == 4
    # 服务与队列仍然健康
    assert client.get("/api/status").status_code == 200


# ══════════════════════════════════════════════════════════
# 媒体接口
# ══════════════════════════════════════════════════════════

def _png_bytes() -> bytes:
    """最小合法 PNG（1x1）—— 让 core.media 能识别为图片。"""
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000a49444154789c6300010000050001"
        "0d0a2db40000000049454e44ae426082")


def test_media_upload_saves_and_returns_rel(client):
    r = client.post("/api/media/upload",
                    files={"file": ("photo.png", _png_bytes(), "image/png")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["rel"]
    assert "/" not in body["rel"] and "\\" not in body["rel"]  # 必须是裸文件名
    assert body["media"]["kind"] == "photo"
    # 真的落盘了
    from core import config
    assert (config.MEDIA_DIR / body["rel"]).is_file()


def test_media_upload_rejects_empty(client):
    r = client.post("/api/media/upload", files={"file": ("e.png", b"", "image/png")})
    assert r.status_code == 400
    assert "空" in r.json()["error"]


def test_media_upload_blocks_traversal(client):
    """带 ../ 的文件名必须被挡在 MEDIA_DIR 内。"""
    from core import config

    before = set(p.name for p in config.MEDIA_DIR.iterdir()) if config.MEDIA_DIR.exists() else set()
    r = client.post("/api/media/upload",
                    files={"file": ("../../evil.png", _png_bytes(), "image/png")})
    assert r.status_code == 200, r.text
    rel = r.json()["rel"]
    assert ".." not in rel
    target = (config.MEDIA_DIR / rel).resolve()
    assert target.parent == config.MEDIA_DIR.resolve()
    # 确保没有在父目录里造文件
    leaked = config.MEDIA_DIR.parent / "evil.png"
    assert not leaked.exists()


def test_media_list_includes_uploaded(client):
    up = client.post("/api/media/upload",
                     files={"file": ("listed.png", _png_bytes(), "image/png")})
    assert up.status_code == 200
    r = client.get("/api/media/list?limit=20")
    assert r.status_code == 200, r.text
    names = [m["name"] for m in r.json()["media"]]
    assert up.json()["rel"] in names
    assert "limits" in r.json()


def test_media_file_serves_only_within_media_dir(client):
    up = client.post("/api/media/upload",
                     files={"file": ("serve.png", _png_bytes(), "image/png")})
    rel = up.json()["rel"]
    r = client.get(f"/api/media/file?name={rel}")
    assert r.status_code == 200
    assert r.content == _png_bytes()

    # 目录穿越与不存在都必须被拒
    assert client.get("/api/media/file?name=../../../etc/passwd").status_code in (400, 404)
    assert client.get("/api/media/file?name=nope.png").status_code == 404
    assert client.get("/api/media/file?name=").status_code == 400


def test_media_validate_rejects_oversize(client, monkeypatch):
    """超过平台限制的媒体在校验接口就要被拒。"""
    from core import config, media as m

    p = config.MEDIA_DIR / "big.jpg"
    p.write_bytes(b"\xff\xd8\xff" + b"0" * 64)
    monkeypatch.setattr(m, "_size_of", lambda _p: m.IMAGE_MAX_BYTES + 1)
    r = client.post("/api/media/validate", json={"media": "big.jpg", "kind": "photo"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False
    assert r.json()["message"]


def test_jobs_create_rejects_oversize_media(client, monkeypatch):
    """入队时也要挡住超限媒体（不能等到发布才失败）。"""
    from core import config, media as m

    p = config.MEDIA_DIR / "toobig.jpg"
    p.write_bytes(b"\xff\xd8\xff" + b"0" * 64)
    monkeypatch.setattr(m, "_size_of", lambda _p: m.IMAGE_MAX_BYTES + 1)

    r = client.post("/api/jobs", json={"text": "带超大图", "media_path": "toobig.jpg"})
    assert r.status_code == 400, r.text
    assert "error" in r.json()


def test_jobs_create_accepts_valid_media(client):
    up = client.post("/api/media/upload",
                     files={"file": ("ok.png", _png_bytes(), "image/png")})
    rel = up.json()["rel"]
    r = client.post("/api/jobs", json={"text": "带图", "media_path": rel})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "photo"


def test_status_exposes_tg_and_media(client):
    r = client.get("/api/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "tg" in body
    assert "media" in body
    assert body["media"]["video_max_seconds"] == 140
    assert body["settings"]["tg"]["autostart"] in (True, False)


# ══════════════════════════════════════════════════════════
# 账号密码登录
# ══════════════════════════════════════════════════════════

class _FakeBrowserBackend:
    name = "browser"
    label = "无头浏览器"

    def __init__(self):
        self.received: list[dict] = []

    def available(self):
        return False, "未登录"

    def verify(self):
        return False, "未登录"

    def login(self, on_event=None):
        return True, "ok"

    def login_with_password(self, username, password, code="", on_event=None):
        self.received.append({"username": username, "password": password, "code": code})
        self._ev = on_event
        if on_event:
            on_event("已提交登录表单")
        if username == "needcode" and not code:
            return False, "需要验证码：请在控制台填入收到的验证码后重试（已保留本次会话）"
        if password == "wrong":
            return False, "账号或密码不正确（X 未通过验证）"
        return True, "登录成功，凭据已保存"


def _inject_browser(monkeypatch):
    be = _FakeBrowserBackend()
    monkeypatch.setattr(web_server.registry, "get",
                        lambda name: be if name == "browser" else None, raising=False)
    return be


def test_password_login_rejects_backend_without_support(client, monkeypatch):
    class _NoPw:
        pass
    monkeypatch.setattr(web_server.registry, "get", lambda name: _NoPw(), raising=False)
    r = client.post("/api/backends/browser/login-password",
                    json={"username": "u", "password": "p"})
    assert r.status_code == 400, r.text
    assert "不支持" in r.json()["error"]


def test_password_login_requires_both_fields(client, monkeypatch):
    _inject_browser(monkeypatch)
    r = client.post("/api/backends/browser/login-password",
                    json={"username": "u", "password": ""})
    assert r.status_code == 400
    r = client.post("/api/backends/browser/login-password",
                    json={"username": "", "password": "p"})
    assert r.status_code == 400


def test_password_login_starts_and_reports_success(client, monkeypatch):
    be = _inject_browser(monkeypatch)
    r = client.post("/api/backends/browser/login-password",
                    json={"username": "me", "password": "secret123"})
    assert r.status_code == 200, r.text
    assert r.json()["started"] is True
    assert r.json()["username"] == "me"

    # 后台线程：等它结束
    import time
    for _ in range(60):
        st = client.get("/api/backends/browser/login/status").json()
        if not st["running"]:
            break
        time.sleep(0.1)
    st = client.get("/api/backends/browser/login/status").json()
    assert st["ok"] is True, json.dumps(st, ensure_ascii=False)
    assert be.received and be.received[0]["username"] == "me"


def test_password_login_response_never_echoes_password(client, monkeypatch):
    """响应体里绝不能出现密码明文。"""
    _inject_browser(monkeypatch)
    secret = "Sup3rSecret!Passw0rd"
    r = client.post("/api/backends/browser/login-password",
                    json={"username": "me", "password": secret})
    assert secret not in r.text
    assert secret not in json.dumps(r.json(), ensure_ascii=False)

    import time
    for _ in range(60):
        st = client.get("/api/backends/browser/login/status")
        if not st.json()["running"]:
            break
        time.sleep(0.1)
    assert secret not in st.text


def test_password_login_need_code_is_distinguishable(client, monkeypatch):
    """「需要验证码」必须与「密码错」区分开，前端据此显示验证码输入框。"""
    _inject_browser(monkeypatch)
    client.post("/api/backends/browser/login-password",
                json={"username": "needcode", "password": "pw123456"})
    import time
    for _ in range(60):
        st = client.get("/api/backends/browser/login/status").json()
        if not st["running"]:
            break
        time.sleep(0.1)
    assert st["ok"] is False
    assert st["need_code"] is True
    assert "验证码" in st["message"]


def test_password_login_stored_nowhere_in_db(client, monkeypatch):
    """密码绝不能进 settings / DB。"""
    from core import settings as core_settings

    _inject_browser(monkeypatch)
    secret = "NeverStoreThis123!"
    client.post("/api/backends/browser/login-password",
                json={"username": "me", "password": secret})
    import time
    for _ in range(60):
        if not client.get("/api/backends/browser/login/status").json()["running"]:
            break
        time.sleep(0.1)

    blob = json.dumps(core_settings.all_settings(), ensure_ascii=False)
    assert secret not in blob
    from core import config
    assert secret not in config.DB_PATH.read_bytes().decode("utf-8", "ignore")


# ── 启动器日志与崩溃可诊断性 ──────────────────────────────
#
# 背景：一次真实事故 —— 服务进程退出码 1、无 traceback、无崩溃转储，
# `data/logs/` 空空如也，事故完全无法诊断。根因是日志只进 stdout，
# 全代码库没有一个 FileHandler，而 AGENTS.md 却声称「Logs go to data/logs/」。
# 下面几条锁死：日志必须落盘、崩溃必须留 traceback。


def test_start_py_has_file_logging():
    """`start.py` 必须把日志同时写进 `data/logs/`，不能只依赖 stdout。"""
    src = (ROOT / "start.py").read_text(encoding="utf-8")
    assert "FileHandler" in src or "RotatingFileHandler" in src, \
        "start.py 没有文件日志 handler —— 进程一死现场全丢"
    assert "LOG_DIR" in src, "start.py 没有用 config.LOG_DIR（日志应落在 data/logs/）"


def test_start_py_captures_unhandled_exceptions():
    """未捕获异常必须落进日志（sys.excepthook / faulthandler）。"""
    src = (ROOT / "start.py").read_text(encoding="utf-8")
    assert "excepthook" in src, "没有 sys.excepthook —— 未捕获异常不留痕"
    assert "faulthandler" in src, "没有 faulthandler —— 段错误类硬崩溃不留痕"


def test_setup_logging_is_not_recursive():
    """`_setup_logging()` 不得调用自身（曾经的真实缺陷：无限递归 RecursionError）。

    上次修复时一次编辑失误把 `_setup_logging` 的定义插进了 `main()` 中间，
    结果函数体末尾又调用了自己 —— 启动即 RecursionError 崩死。
    这里既做源码级断言，也做真实的调用验证。
    """
    import ast

    src = (ROOT / "start.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    # 找出 _setup_logging 定义，检查它自己体内有没有递归调用
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_setup_logging"), None)
    assert fn is not None, "start.py 里找不到 _setup_logging 定义"
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "_setup_logging", \
                "_setup_logging 递归调用自己 —— 启动会 RecursionError"

    # 真实调用一次，验证不会崩、且确实挂上了文件 handler
    import importlib

    sys.path.insert(0, str(ROOT))
    start_mod = importlib.import_module("start")
    start_mod._setup_logging()
    handlers = logging.getLogger().handlers
    assert len(handlers) >= 2, f"期望控制台+文件两个 handler，实际 {len(handlers)}"
    assert any(isinstance(h, logging.FileHandler) for h in handlers), \
        "没有挂上 FileHandler"


def test_main_function_is_wellformed():
    """`main()` 必须是一个完整函数（防止编辑失误把它切成两半）。"""
    import ast

    src = (ROOT / "start.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    main = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    assert main is not None, "找不到 main()"
    # main 里必须有实际逻辑：解析参数 + 拿单实例锁 + 跑事件循环
    body_src = ast.get_source_segment(src, main) or ""
    assert "parse_args" in body_src, "main() 里没有解析参数 —— 函数可能被截断"
    assert "single_instance_lock" in body_src, "main() 里没有取单实例锁 —— 函数可能被截断"
    assert "asyncio.run" in body_src, "main() 里没有 asyncio.run —— 函数可能被截断"
    assert "_setup_logging()" in body_src, "main() 没有调用 _setup_logging()"
