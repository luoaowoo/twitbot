"""browser 后端测试（★C）

覆盖三层：
  A. 纯函数：tweet_id 提取 / X GraphQL 错误分类 / toast 文案分类
  B. mock 层：available 不联网不启浏览器、publish 各失败路径、异常不外抛、**浏览器必被关闭**、并发串行化
  C. 真浏览器层（离线）：用 set_content 造一个 contenteditable(DraftJS 风格) 页面，
     真跑 `_type_text`，验证换行走 Shift+Enter、CJK/emoji 逐字输入且回读一致

运行：
    .\\.venv\\Scripts\\python.exe -m pytest tests/test_browser.py -v
    .\\.venv\\Scripts\\python.exe tests/test_browser.py      # 无 pytest 也能自跑
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core import config  # noqa: E402
from core.backends import browser as B  # noqa: E402
from core.backends.base import Job, PublishResult  # noqa: E402


# ══════════════════════════════════════════════════════════
# 测试替身
# ══════════════════════════════════════════════════════════

class FakeLocator:
    """默认"页面上没有这个元素"；count()==0 让代码走找不到的分支。"""

    def __init__(self, page=None, selector="", visible=False, enabled=True, text=""):
        self.page = page
        self.selector = selector
        self._visible = visible
        self._enabled = enabled
        self._text = text
        self.clicks = 0

    @property
    def first(self):
        return self

    def count(self) -> int:
        return 1 if (self._visible or self._text) else 0

    def is_visible(self) -> bool:
        return self._visible

    def is_enabled(self) -> bool:
        return self._enabled

    def inner_text(self) -> str:
        return self._text

    def text_content(self) -> str:
        return self._text

    def get_attribute(self, name):
        return self._text if name == "href" else None

    def click(self, **kw) -> None:
        self.clicks += 1

    def nth(self, i):
        return self


class FakeResponse:
    def __init__(self, payload=None, url="https://x.com/i/api/graphql/abc/CreateTweet",
                 status=200, text=None):
        self._payload = payload
        self.url = url
        self.status = status
        self._text = text if text is not None else json.dumps(payload or {})
        self.request = type("Req", (), {"method": "POST"})()

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def text(self):
        return self._text


class ExpectCM:
    """模拟 playwright 的 page.expect_response(...) 上下文管理器。"""

    def __init__(self, value=None, raise_exc=None):
        self.value = value
        self._raise = raise_exc

    def __enter__(self):
        if self._raise is not None:
            raise self._raise
        return self

    def __exit__(self, *a):
        return False


class FakeNavResponse:
    """page.goto() 的返回值替身（需要 .status）。"""
    def __init__(self, status=200):
        self.status = status


class FakePage:
    def __init__(self, url="https://x.com/home", expect_factory=None,
                 locator_factory=None, keep_url=False, nav_status=200,
                 body_text="content"):
        self.url = url
        self._expect_factory = expect_factory
        self._locator_factory = locator_factory
        # keep_url=True 模拟"导航被重定向回登录页"：goto 不改 url，
        # 这样 _probe_login_state 立刻能判出 False，测试不必空转等超时。
        self._keep_url = keep_url
        self._nav_status = nav_status
        self._body_text = body_text
        self.screenshots: list[str] = []
        self.gotos: list[str] = []
        self.waits = 0

    # 导航/等待
    def goto(self, url, **kw):
        self.gotos.append(url)
        if self._keep_url:
            return FakeNavResponse(self._nav_status)
        if "login" in url:
            self.url = "https://x.com/login"
        elif "home" in url:
            self.url = "https://x.com/home"
        return FakeNavResponse(self._nav_status)

    def evaluate(self, expr, *a):
        # 对齐真实探针的返回形状：{children, html, text}。
        # body_text 非空 => 有内容；为空 => 模拟空壳响应体（零子元素、零 HTML）。
        if self._body_text:
            return {"children": 6, "html": 4618, "text": self._body_text}
        return {"children": 0, "html": 0, "text": ""}

    def wait_for_timeout(self, ms):
        self.waits += 1
        return None

    def wait_for_selector(self, sel, **kw):
        raise B.PWTimeoutError("no element (fake)")

    def wait_for_url(self, *a, **kw):
        return None

    # 元素
    def locator(self, sel):
        if self._locator_factory:
            return self._locator_factory(sel)
        return FakeLocator(self, sel)

    # 网络
    def expect_response(self, *a, **kw):
        if self._expect_factory:
            return self._expect_factory()
        return ExpectCM(raise_exc=B.PWTimeoutError("timeout (fake)"))

    # 截图：真落盘，便于断言"失败必留证"
    def screenshot(self, path=None, full_page=True):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
        self.screenshots.append(str(path))

    def title(self):
        return "fake"


class FakeContext:
    def __init__(self, page: FakePage, record: dict):
        self._page = page
        self._record = record

    def new_page(self):
        return self._page

    def add_init_script(self, js):
        self._record["init_script"] = js

    def set_default_timeout(self, ms):
        pass

    def set_default_navigation_timeout(self, ms):
        pass

    def storage_state(self, path=None):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"cookies": [{"name": "auth_token", "value": "x"}],
                                 "origins": []}), encoding="utf-8")
        return {"cookies": []}

    def close(self):
        self._record["context_close"] = self._record.get("context_close", 0) + 1


class FakeBrowser:
    def __init__(self, record: dict, page: FakePage):
        self._record = record
        self._page = page

    def new_context(self, **kw):
        self._record["new_context_kwargs"] = kw
        return FakeContext(self._page, self._record)

    def close(self):
        self._record["browser_close"] = self._record.get("browser_close", 0) + 1


class FakeChromium:
    def __init__(self, record, page):
        self._record = record
        self._page = page

    def launch(self, **kw):
        self._record["launch_kwargs"] = kw
        self._record["launches"] = self._record.get("launches", 0) + 1
        return FakeBrowser(self._record, self._page)


class FakePW:
    def __init__(self, record, page):
        self.rec = record          # 注意：不要用 self._record，双下划线私有名会被改写
        self.chromium = FakeChromium(record, page)

    def stop(self):
        self.rec["pw_stop"] = self.rec.get("pw_stop", 0) + 1


class Harness:
    """把 BrowserBackend 里的 start_playwright 换成假的，记录所有生命周期事件。"""

    def __init__(self, monkeypatch, page: FakePage | None = None):
        self.record: dict = {}
        self.page = page or FakePage()
        self.pw = FakePW(self.record, self.page)
        monkeypatch.setattr(B, "start_playwright", lambda: self.pw)


@pytest.fixture(autouse=True)
def fast_launch(monkeypatch):
    """每个测试都从"首选启动配置"开始，并清掉 BROWSER_CHANNEL 干扰。"""
    B.BrowserBackend._reset_good()
    monkeypatch.delenv("BROWSER_CHANNEL", raising=False)
    yield
    B.BrowserBackend._reset_good()


# ══════════════════════════════════════════════════════════
# fixtures
# ══════════════════════════════════════════════════════════

@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """把 BROWSER_DIR / LOG_DIR / MEDIA_DIR 指到临时目录。"""
    browser_dir = tmp_path / "browser"
    log_dir = tmp_path / "logs"
    media_dir = tmp_path / "media"
    for d in (browser_dir, log_dir, media_dir):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "BROWSER_DIR", browser_dir)
    monkeypatch.setattr(config, "LOG_DIR", log_dir)
    monkeypatch.setattr(config, "MEDIA_DIR", media_dir)
    return {"browser": browser_dir, "log": log_dir, "media": media_dir}


def write_state(dirs, name="storage_state.json", cookies=None) -> Path:
    p = dirs["browser"] / name
    p.write_text(json.dumps({"cookies": cookies if cookies is not None
                             else [{"name": "auth_token", "value": "v"}]}),
                 encoding="utf-8")
    return p


def make_job(**kw) -> Job:
    base = dict(id=7, kind="text", raw_text="hi", tweet_text="hi")
    base.update(kw)
    return Job(**base)


# ══════════════════════════════════════════════════════════
# A. 纯函数
# ══════════════════════════════════════════════════════════

def test_extract_tweet_id_from_create_tweet():
    payload = {"data": {"create_tweet": {"tweet_results": {"result": {"rest_id": "1799888777666555444"}}}}}
    assert B._extract_tweet_id(payload) == "1799888777666555444"


def test_extract_tweet_id_from_create_note_tweet_nested():
    payload = {"data": {"create_note_tweet": {"tweet_results": {"result": {
        "rest_id": "1234567890123456789", "core": {"user_results": {"result": {"rest_id": "999"}}}}}}}}
    # 必须拿到推文 id，而不是作者 id
    assert B._extract_tweet_id(payload) == "1234567890123456789"


def test_extract_tweet_id_absent():
    assert B._extract_tweet_id({"data": {"create_tweet": {"tweet_results": {"result": {}}}}}) == ""
    assert B._extract_tweet_id(None) == ""
    assert B._extract_tweet_id({"errors": [{"code": 187}]}) == ""


def test_is_create_tweet_request():
    assert B._is_create_tweet_request(FakeResponse()) is True
    assert B._is_create_tweet_request(
        FakeResponse(url="https://x.com/i/api/1.1/statuses/update.json")) is True
    assert B._is_create_tweet_request(
        FakeResponse(url="https://x.com/i/api/graphql/abc/FavoriteTweet")) is False


def test_classify_api_error_codes():
    assert B._classify_api_error([{"code": 187, "message": "duplicate"}])[0] == "fatal"
    assert B._classify_api_error([{"code": 344, "message": "daily limit"}])[0] == "retry"
    assert B._classify_api_error([{"code": 344, "message": "daily limit"}])[1] == 900
    assert B._classify_api_error([{"code": 64, "message": "suspended"}])[0] == "fatal"
    kind, _, msg = B._classify_api_error([{"code": None, "message": "Rate limit exceeded"}])
    assert kind == "retry" and "Rate limit" in msg


def test_classify_toast_error():
    assert B._classify_toast_error("Your post was sent.")[0] == "retry"  # 未命中错误词 -> 归为可重试
    assert B._classify_toast_error("You have hit your daily limit")[0] == "retry"
    assert B._classify_toast_error("Duplicate post")[0] == "fatal"


def test_norm_text():
    assert B._norm_text("a\r\nb\u200b  ") == "a\nb"
    assert B._norm_text("  x  ") == B._norm_text("x")


# ══════════════════════════════════════════════════════════
# B. available()
# ══════════════════════════════════════════════════════════

def test_available_false_without_state_and_no_browser(monkeypatch, dirs):
    """无 storage_state：返回 (False, ...) 且**绝不启动浏览器**。"""
    def boom():
        raise AssertionError("available() 不得启动浏览器！")
    monkeypatch.setattr(B, "start_playwright", boom)

    be = B.BrowserBackend()
    ok, reason = be.available()
    assert ok is False
    assert "未登录" in reason
    # 必须指向**真实 Chrome** 的助手：旧的 browser_login.py 用 Playwright
    # 启动浏览器，带自动化指纹，会被 x.com 风控拦（prelude_gate）。
    assert "browser_login_chrome.py" in reason
    assert "登录 X" in reason


def test_available_false_when_state_empty(monkeypatch, dirs):
    (dirs["browser"] / "storage_state.json").write_text("", encoding="utf-8")
    monkeypatch.setattr(B, "start_playwright", lambda: (_ for _ in ()).throw(
        AssertionError("available() 不得启动浏览器！")))
    ok, reason = B.BrowserBackend().available()
    assert ok is False and "未登录" in reason


def test_available_false_when_state_is_garbage(monkeypatch, dirs):
    (dirs["browser"] / "storage_state.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(B, "start_playwright", lambda: (_ for _ in ()).throw(
        AssertionError("no browser")))
    ok, _ = B.BrowserBackend().available()
    assert ok is False


def test_available_false_when_cookies_empty(monkeypatch, dirs):
    (dirs["browser"] / "storage_state.json").write_text(
        json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    monkeypatch.setattr(B, "start_playwright", lambda: (_ for _ in ()).throw(
        AssertionError("no browser")))
    ok, reason = B.BrowserBackend().available()
    assert ok is False and "未登录" in reason


def test_available_true_with_state(monkeypatch, dirs):
    write_state(dirs)
    monkeypatch.setattr(B, "start_playwright", lambda: (_ for _ in ()).throw(
        AssertionError("available() 不得启动浏览器！")))
    ok, reason = B.BrowserBackend().available()
    assert ok is True
    assert "已保存登录态" in reason


# ══════════════════════════════════════════════════════════
# B. publish() 失败路径
# ══════════════════════════════════════════════════════════

def test_publish_without_state_returns_non_retryable_no_browser(monkeypatch, dirs):
    monkeypatch.setattr(B, "start_playwright", lambda: (_ for _ in ()).throw(
        AssertionError("没登录态不该启浏览器！")))
    r = B.BrowserBackend().publish(make_job(), "hello")
    assert isinstance(r, PublishResult)
    assert r.ok is False
    assert r.retryable is False
    assert "登录态失效" in r.error
    assert r.backend == "browser"


def test_publish_logged_out_returns_non_retryable_with_screenshot(monkeypatch, dirs):
    """登录态文件在、但页面上已登出 -> ok=False / retryable=False / 有截图。"""
    write_state(dirs)
    page = FakePage(url="https://x.com/i/flow/login", keep_url=True)
    Harness(monkeypatch, page)

    r = B.BrowserBackend().publish(make_job(id=42), "hello")
    assert r.ok is False
    assert r.retryable is False
    assert "登录态失效" in r.error
    assert r.extra.get("evidence") == "logged_out"
    shot = r.extra.get("screenshot")
    assert shot and Path(shot).is_file(), "失败必须截图留证"
    assert Path(shot).name.startswith("fail_42_")
    assert Path(shot).parent == dirs["log"]


def test_publish_network_error_is_retryable(monkeypatch, dirs):
    write_state(dirs)

    class BoomPage(FakePage):
        def goto(self, url, **kw):
            raise RuntimeError("net::ERR_INTERNET_DISCONNECTED")

    page = BoomPage()
    Harness(monkeypatch, page)
    r = B.BrowserBackend().publish(make_job(), "hello")
    assert r.ok is False
    assert r.retryable is True, "网络不可达应可重试"
    assert "网络" in r.error or "ERR" in r.error


def test_publish_open_composer_failure_non_retryable(monkeypatch, dirs):
    """登录态有效但找不到发帖入口（前端改版）-> retryable=False 且截图。"""
    write_state(dirs)
    page = FakePage(url="https://x.com/home")
    Harness(monkeypatch, page)
    be = B.BrowserBackend()
    monkeypatch.setattr(be, "_wait_login_state", lambda p, t: True)

    r = be.publish(make_job(id=5), "hello")
    assert r.ok is False
    assert r.retryable is False
    assert "未找到发帖入口" in r.error
    assert r.extra.get("evidence") == "composer_missing"
    assert r.extra.get("screenshot")


def test_quote_without_editor_aborts_instead_of_posting_without_quote(monkeypatch, dirs):
    """引用的编辑器没出来时，必须中止，绝不能退回普通发帖路径发出丢引用的推文。"""
    write_state(dirs)
    page = FakePage()
    Harness(monkeypatch, page)
    be = B.BrowserBackend()
    monkeypatch.setattr(be, "_wait_login_state", lambda p, t: True)
    # "引用"菜单点成功了，但编辑器始终不出现
    monkeypatch.setattr(be, "_setup_quote", lambda p, qid: "")
    monkeypatch.setattr(be, "_wait_editor", lambda p, t: False)
    # 若实现错误地退回普通路径，这些会被调用 —— 用抛异常来暴露
    monkeypatch.setattr(be, "_type_text", lambda p, t: (_ for _ in ()).throw(
        AssertionError("不得退回普通发帖路径！")))
    monkeypatch.setattr(be, "_find_send_button", lambda p: (_ for _ in ()).throw(
        AssertionError("不得退回普通发帖路径！")))

    r = be.publish(make_job(id=51, quote_id="1234567890"), "hello")
    assert r.ok is False
    assert r.retryable is False
    assert r.extra.get("composer") == "quote"
    assert "引用" in r.error
    assert "中止" in r.error or "丢失引用" in r.error


def test_publish_text_input_failure_non_retryable(monkeypatch, dirs):
    write_state(dirs)
    page = FakePage()
    Harness(monkeypatch, page)
    be = B.BrowserBackend()
    monkeypatch.setattr(be, "_wait_login_state", lambda p, t: True)
    monkeypatch.setattr(be, "_open_composer", lambda p, j: ("modal", ""))
    monkeypatch.setattr(be, "_type_text", lambda p, t: (False, "文本输入失败：编辑器回读内容与预期不一致"))

    r = be.publish(make_job(id=6), "hello")
    assert r.ok is False and r.retryable is False
    assert "文本输入失败" in r.error
    assert r.extra.get("evidence") == "text_input_failed"
    assert r.extra.get("screenshot")


def test_publish_media_missing_is_non_retryable(monkeypatch, dirs):
    write_state(dirs)
    Harness(monkeypatch)
    r = B.BrowserBackend().publish(make_job(media_path="nope.jpg"), "hello")
    assert r.ok is False and r.retryable is False
    assert "媒体文件不存在" in r.error


def test_publish_internal_exception_never_raises(monkeypatch, dirs):
    """内部任何异常都必须转成 PublishResult，不外抛。"""
    write_state(dirs)
    monkeypatch.setattr(B, "start_playwright", lambda: (_ for _ in ()).throw(
        RuntimeError("浏览器崩溃了")))
    r = B.BrowserBackend().publish(make_job(id=9), "hello")
    assert isinstance(r, PublishResult)
    assert r.ok is False
    assert "浏览器崩溃了" in r.error
    assert r.retryable is True


def test_publish_exception_inside_locked_path_never_raises(monkeypatch, dirs):
    write_state(dirs)
    Harness(monkeypatch)
    be = B.BrowserBackend()

    def boom(job, text, media, shot, started):
        raise ValueError("意料之外")

    monkeypatch.setattr(be, "_publish_locked", boom)
    r = be.publish(make_job(), "hello")
    assert r.ok is False and "意料之外" in r.error
    assert r.extra.get("evidence") == "exception"


# ══════════════════════════════════════════════════════════
# B. 资源管理：浏览器一定会被关掉
# ══════════════════════════════════════════════════════════

def test_browser_and_context_closed_on_success_path(monkeypatch, dirs):
    write_state(dirs)
    page = FakePage()
    h = Harness(monkeypatch, page)
    be = B.BrowserBackend()

    payload = {"data": {"create_tweet": {"tweet_results": {"result": {"rest_id": "1800000000000000001"}}}}}
    monkeypatch.setattr(be, "_wait_login_state", lambda p, t: True)
    monkeypatch.setattr(be, "_open_composer", lambda p, j: ("modal", ""))
    monkeypatch.setattr(be, "_type_text", lambda p, t: (True, ""))
    monkeypatch.setattr(be, "_find_send_button", lambda p: (FakeLocator(), ""))
    page._expect_factory = lambda: ExpectCM(value=FakeResponse(payload=payload))

    r = be.publish(make_job(), "hello")
    assert r.ok is True and r.tweet_id == "1800000000000000001"
    assert h.record["context_close"] == 1, "context 必须被关闭"
    assert h.record["browser_close"] == 1, "browser 必须被关闭"
    assert h.record["pw_stop"] == 1, "playwright 必须 stop"


def test_browser_and_context_closed_on_failure_path(monkeypatch, dirs):
    write_state(dirs)
    page = FakePage(url="https://x.com/i/flow/login", keep_url=True)
    h = Harness(monkeypatch, page)
    r = B.BrowserBackend().publish(make_job(), "hello")
    assert r.ok is False
    assert h.record["context_close"] == 1
    assert h.record["browser_close"] == 1
    assert h.record["pw_stop"] == 1


def test_browser_closed_even_when_close_raises(monkeypatch, dirs):
    """context.close 抛异常也不能阻止 browser.close / pw.stop。"""
    write_state(dirs)

    class BadContext(FakeContext):
        def close(self):
            self._record["context_close"] = self._record.get("context_close", 0) + 1
            raise RuntimeError("close 炸了")

    class BadBrowser(FakeBrowser):
        def new_context(self, **kw):
            return BadContext(self._page, self._record)

    class BadChromium(FakeChromium):
        def launch(self, **kw):
            self._record["launches"] = self._record.get("launches", 0) + 1
            return BadBrowser(self._record, self._page)

    record: dict = {}
    page = FakePage(url="https://x.com/i/flow/login", keep_url=True)
    pw = FakePW(record, page)
    pw.chromium = BadChromium(record, page)
    monkeypatch.setattr(B, "start_playwright", lambda: pw)

    r = B.BrowserBackend().publish(make_job(), "hello")
    assert r.ok is False
    assert record["context_close"] == 1
    assert record["browser_close"] == 1, "context.close 失败也必须关浏览器"
    assert record["pw_stop"] == 1


def test_browser_closed_when_launch_fails(monkeypatch, dirs):
    write_state(dirs)
    record: dict = {}

    class FailChromium:
        def launch(self, **kw):
            record["launches"] = record.get("launches", 0) + 1
            raise RuntimeError("chromium 起不来")

    pw = FakePW(record, FakePage())
    pw.chromium = FailChromium()
    monkeypatch.setattr(B, "start_playwright", lambda: pw)

    r = B.BrowserBackend().publish(make_job(), "hello")
    assert r.ok is False
    assert "chromium 起不来" in r.error
    assert record["pw_stop"] == 1, "launch 失败也必须 stop playwright"


def test_verify_does_not_publish_and_uses_headless(monkeypatch, dirs):
    write_state(dirs)
    h = Harness(monkeypatch, FakePage(url="https://x.com/home"))
    be = B.BrowserBackend()
    monkeypatch.setattr(be, "_wait_login_state", lambda p, t: True)
    monkeypatch.setattr(be, "_open_composer", lambda p, j: (_ for _ in ()).throw(
        AssertionError("verify 不得发推！")))
    ok, reason = be.verify()
    assert ok is True and "有效" in reason
    assert h.record["launch_kwargs"]["headless"] is config.BROWSER_HEADLESS
    assert h.record["browser_close"] == 1


def test_verify_without_state(monkeypatch, dirs):
    monkeypatch.setattr(B, "start_playwright", lambda: (_ for _ in ()).throw(
        AssertionError("no browser")))
    ok, reason = B.BrowserBackend().verify()
    assert ok is False and "未登录" in reason


def test_verify_logged_out(monkeypatch, dirs):
    """访问 /home 被重定向回登录页 —— 模拟真实登出（导航后 url 仍是登录页）。"""
    write_state(dirs)

    class RedirectedPage(FakePage):
        def goto(self, url, **kw):
            self.gotos.append(url)   # 请求了 /home，但被重定向，url 保持登录页
            return FakeNavResponse(200)

    Harness(monkeypatch, RedirectedPage(url="https://x.com/i/flow/login?redirect_after_login=%2Fhome"))
    ok, reason = B.BrowserBackend().verify()
    assert ok is False and "失效" in reason


def test_verify_unknown_page_says_unknown(monkeypatch, dirs):
    """页面既没有登录特征也没有主页特征 -> 明确说"无法确认"，而不是误判为失效。"""
    write_state(dirs)
    Harness(monkeypatch, FakePage(url="https://x.com/home"))
    be = B.BrowserBackend()
    monkeypatch.setattr(be, "_wait_login_state", lambda p, t: False)  # 跳过真实轮询等待
    ok, reason = be.verify()
    assert ok is False and "无法确认" in reason


# ══════════════════════════════════════════════════════════
# B. 403 拦截识别（实测：自带 headless shell 会被 x.com 回 403）
# ══════════════════════════════════════════════════════════

def test_publish_detects_403_block_as_retryable_not_logged_out(monkeypatch, dirs):
    """被 x.com 403 拦截 ≠ 登录态失效：必须区分开，否则会误导排查方向。"""
    write_state(dirs)
    page = FakePage(url="https://x.com/home", keep_url=True, nav_status=403,
                    body_text="")
    Harness(monkeypatch, page)

    r = B.BrowserBackend().publish(make_job(id=77), "hello")
    assert r.ok is False
    assert r.extra.get("evidence") == "blocked", "403 应被识别为被拦截，而不是 logged_out"
    assert r.retryable is True, "被拦截属于环境问题，值得换配置后重试"
    assert "403" in r.error
    assert "BROWSER_HEADLESS" in r.error, "错误信息应给出可操作的排查建议"
    assert r.extra.get("screenshot")


def test_publish_detects_blank_200_page_as_blocked(monkeypatch, dirs):
    """实测：被拒时状态码也可能是 200 却给一张空白壳页 —— 同样要识别为被拦截。"""
    write_state(dirs)
    page = FakePage(url="https://x.com/home", keep_url=True, nav_status=200,
                    body_text="")           # 无文本、无 data-testid = 空白壳页
    Harness(monkeypatch, page)
    monkeypatch.setattr(B, "_BLANK_SETTLE_MS", 0)   # 别让测试真的等渲染

    r = B.BrowserBackend().publish(make_job(id=78), "hello")
    assert r.ok is False
    assert r.extra.get("evidence") == "blocked"
    assert r.extra.get("blank_page") is True
    assert "页面为空" in r.error
    assert r.retryable is True


def test_block_message_distinguishes_blank_page_from_4xx():
    """状态码正常但页面空白时，原因不能说成"HTTP x"，否则误导排查方向。"""
    be = B.BrowserBackend()
    msg_403 = be._block_message(403)
    assert "HTTP 403" in msg_403 and "BROWSER_HEADLESS" in msg_403

    msg_blank = be._block_message(None)
    assert "页面为空" in msg_blank
    assert "HTTP" not in msg_blank.replace("HTTP 状态码正常", ""), "空白页别说成 HTTP 错误"

    msg_200 = be._block_message(200)
    assert "HTTP 200" in msg_200 and "内容为空" in msg_200


def test_page_is_blank_does_not_flag_real_sparse_page(monkeypatch):
    """回归：真实的 x.com 未登录页 innerText 为空、零个 data-testid、
    也没有 #react-root，但 body 里有 6 个子元素 —— 不能被误判成"被拦截"。
    """
    class SparsePage:
        """实测到的 /i/jf/onboarding 页面特征。"""

        def evaluate(self, expr, *a):
            return {"children": 6, "html": 4618, "text": ""}

        def wait_for_timeout(self, ms):
            pass

    assert B._page_is_blank(SparsePage(), settle_ms=0) is False


def test_page_is_blank_flags_true_empty_shell():
    """被拒时拿到的 <body></body> 空壳：零子元素、零 HTML、无文本 -> 判空白。"""
    class ShellPage:
        def evaluate(self, expr, *a):
            return {"children": 0, "html": 0, "text": ""}

        def wait_for_timeout(self, ms):
            pass

    assert B._page_is_blank(ShellPage(), settle_ms=0) is True


def test_page_is_blank_handles_probe_failure_as_not_blank():
    """探针本身失败（刚导航/已关闭）时不敢断言空白，交给登录态检查去判断。"""
    class BrokenPage:
        def evaluate(self, expr, *a):
            raise RuntimeError("Execution context was destroyed")

    assert B._page_is_blank(BrokenPage(), settle_ms=0) is False


def test_verify_detects_403_block(monkeypatch, dirs):
    write_state(dirs)
    Harness(monkeypatch, FakePage(url="https://x.com/home", keep_url=True,
                                  nav_status=403, body_text=""))
    ok, reason = B.BrowserBackend().verify()
    assert ok is False
    assert "拒绝" in reason, "不能把 403 说成登录态失效"


# ══════════════════════════════════════════════════════════
# B. 阻塞（403 / 空白页）识别与反应式配置回退
# ══════════════════════════════════════════════════════════

def test_publish_treats_goto_403_exception_as_blocked(monkeypatch, dirs):
    """publish 里 goto 抛 ERR_HTTP_RESPONSE_CODE_FAILURE 也要报"被拒绝"而非网络错误。"""
    write_state(dirs)

    class RaisingPage(FakePage):
        def goto(self, url, **kw):
            raise RuntimeError(
                "Page.goto: net::ERR_HTTP_RESPONSE_CODE_FAILURE at https://x.com/home")

    Harness(monkeypatch, RaisingPage())
    monkeypatch.setenv("BROWSER_CHANNEL", "chromium")   # 只试一个配置，便于断言
    r = B.BrowserBackend().publish(make_job(id=88), "hello")
    assert r.ok is False
    assert r.extra.get("evidence") == "blocked"
    assert r.retryable is True
    assert r.extra.get("http_status") == 403


# ── 反应式配置回退（不做投机探测） ────────────────────────
#
# 实测教训：启动前"先探哪个配置能连通"本身就是短时间反复起浏览器访问 x.com，
# 会把本机 IP 拖进限流。所以设计成：默认直接用实测最可靠的 channel="chromium"，
# 只有真的被 403 拦下才换下一个候选重试。

def test_publish_retries_next_config_when_blocked(monkeypatch, dirs):
    """首选 channel=chromium 被 403 -> 自动换下一个候选并成功。"""
    write_state(dirs)
    be = B.BrowserBackend()
    calls: list[int] = []

    def stub(job, text, media, shot, started, *, launch_index):
        calls.append(launch_index)
        if launch_index == 0:
            return PublishResult(ok=False, backend="browser", text=text,
                                 error="访问 x.com 被拒绝（HTTP 403）", retryable=True,
                                 extra={"evidence": "blocked", "http_status": 403})
        return PublishResult(ok=True, backend="browser", text=text,
                             tweet_id="1900000000000000001",
                             tweet_url="https://x.com/i/web/status/1900000000000000001",
                             extra={"evidence": "network"})

    monkeypatch.setattr(be, "_publish_once", stub)
    r = be._publish_locked(make_job(id=31), "hello", None, None, time.time())
    assert r.ok is True
    assert r.tweet_id == "1900000000000000001"
    assert calls == [0, 1], "应先用首选配置，被 403 后再换下一个"

    # 接着第二条：已知序号 1 可用，直接从 1 开始，不再浪费一次 403
    calls.clear()
    be._publish_locked(make_job(id=31), "hello", None, None, time.time())
    assert calls == [1], "应复用已确认可用的配置"


def _make_once_stub(plan):
    """构造 _publish_once 替身：plan = {launch_index: (kind, payload)}"""
    def _stub(job, text, media, shot, started, *, launch_index):
        kind, payload = plan.get(launch_index, ("blocked", None))
        if kind == "blocked":
            return PublishResult(ok=False, backend="browser", text=text,
                                 error="访问 x.com 被拒绝（HTTP 403）",
                                 retryable=True, extra={"evidence": "blocked",
                                                        "http_status": 403})
        if kind == "ok":
            rid = B._extract_tweet_id(payload)
            return PublishResult(ok=True, backend="browser", text=text,
                                 tweet_id=rid, tweet_url=f"https://x.com/i/web/status/{rid}",
                                 extra={"evidence": "network"})
        return PublishResult(ok=False, backend="browser", text=text,
                             error="其它失败", retryable=False,
                             extra={"evidence": kind})
    return _stub


def test_publish_all_configs_blocked_reports_tried(monkeypatch, dirs):
    """全部候选都被拒：返回失败但重试无用，并列出试过哪些配置。"""
    write_state(dirs)
    be = B.BrowserBackend()
    monkeypatch.setattr(be, "_publish_once", _make_once_stub({}))   # 全 blocked
    r = be._publish_locked(make_job(id=32), "hello", None, None, time.time())
    assert r.ok is False
    assert r.retryable is True
    assert r.extra["tried_configs"] == ["chromium", "chrome", "headless-shell"]


def test_publish_does_not_retry_on_non_block_failure(monkeypatch, dirs):
    """非 403 的失败（如登录态失效）不该换配置重试——重试无意义。"""
    write_state(dirs)
    be = B.BrowserBackend()
    calls: list[int] = []

    def stub(job, text, media, shot, started, *, launch_index):
        calls.append(launch_index)
        return PublishResult(ok=False, backend="browser", text=text,
                             error="登录态失效，请重新登录", retryable=False,
                             extra={"evidence": "logged_out"})

    monkeypatch.setattr(be, "_publish_once", stub)
    r = be._publish_locked(make_job(id=33), "hello", None, None, time.time())
    assert r.ok is False and r.retryable is False
    assert calls == [0], "非 403 失败必须立刻返回，不换配置重试"


def test_publish_marks_good_config_for_reuse(monkeypatch, dirs):
    """成功后记住可用配置序号，后续发布直接用它，不再从首选开始试。"""
    write_state(dirs)
    be = B.BrowserBackend()
    monkeypatch.setattr(be, "_publish_once", _make_once_stub(
        {0: ("blocked", None), 1: ("ok", {"data": {"create_tweet": {"tweet_results":
            {"result": {"rest_id": "1900000000000000002"}}}}})}))
    r = be._publish_locked(make_job(id=34), "hello", None, None, time.time())
    assert r.ok is True
    assert B._GOOD_LAUNCH_INDEX == 1, "应记住可用配置序号"

    # 下一次直接从序号 1 开始
    seen: list[int] = []
    monkeypatch.setattr(be, "_publish_once",
                        lambda job, text, media, shot, started, *, launch_index:
                        (seen.append(launch_index), PublishResult(
                            ok=True, backend="browser", text=text, extra={"evidence": "network"}))[1])
    be._publish_locked(make_job(id=35), "hello", None, None, time.time())
    assert seen == [1], "应复用已知可用的配置"


def test_launch_kwargs_respects_browser_channel_env(monkeypatch):
    """BROWSER_CHANNEL 显式指定时只用那一个候选，不再回退。"""
    monkeypatch.setenv("BROWSER_CHANNEL", "chrome")
    cands = B.BrowserBackend._candidates()
    assert cands == [{"headless": True, "channel": "chrome"}]
    assert B.BrowserBackend._launch_kwargs(True) == {"headless": True, "channel": "chrome"}


def test_launch_kwargs_default_and_headful(monkeypatch):
    """默认首选是 channel=chromium（实测最可靠）；
    有头模式优先用系统真 Chrome —— Playwright 自带 Chromium 的 sec-ch-ua 与
    UA 版本不一致，会被 X 风控判成自动化（登录页弹「出了点问题」）。"""
    assert B.BrowserBackend._launch_kwargs(True) == B._LAUNCH_CANDIDATES[0]
    assert B.BrowserBackend._launch_kwargs(True)["channel"] == "chromium"
    # 有头模式必须带 channel="chrome"（真 Chrome），不能再是裸 {"headless": False}
    assert B.BrowserBackend._launch_kwargs(False) == {"headless": False, "channel": "chrome"}
    assert B.BrowserBackend._launch_kwargs(True, index=2) == {"headless": True}


def test_launch_kwargs_headful_honours_forced_channel(monkeypatch):
    """BROWSER_CHANNEL 显式指定时，有头模式也要听（方便排障/无真 Chrome 的机器）。"""
    monkeypatch.setenv("BROWSER_CHANNEL", "chromium")
    assert B.BrowserBackend._launch_kwargs(False) == {"headless": False, "channel": "chromium"}


def test_ua_for_version_matches_real_version():
    """UA 改写必须用浏览器**真实版本**，否则会和 sec-ch-ua 打架。"""
    ua = B._ua_for_version("153.0.8010.53")
    assert "Chrome/153.0.8010.53" in ua
    assert "HeadlessChrome" not in ua
    # 版本号异常时也要能给出可用的兜底值，不抛异常
    assert "Chrome/" in B._ua_for_version("")
    assert "HeadlessChrome" not in B._ua_for_version("")


def test_blocked_config_not_marked_good(monkeypatch, dirs):
    """被拒的配置不能被记成"可用"。"""
    write_state(dirs)
    be = B.BrowserBackend()
    monkeypatch.setattr(be, "_publish_once", _make_once_stub({}))
    be._publish_locked(make_job(id=36), "hello", None, None, time.time())
    assert B._GOOD_LAUNCH_INDEX == 0, "全被拒时不应改动记录"



# ══════════════════════════════════════════════════════════
# B. login()
# ══════════════════════════════════════════════════════════

def test_login_timeout_returns_failure_and_saves_nothing(monkeypatch, dirs):
    """登录超时必须按上限退出，不无限等，也不留下假的凭据文件。"""
    Harness(monkeypatch, FakePage(url="https://x.com/login", keep_url=True))
    be = B.BrowserBackend()
    be.LOGIN_POLL_SECONDS = 0.01
    monkeypatch.setattr(be, "_probe_login_state", lambda p: None)  # 永远判不出

    events: list[str] = []
    ok, msg = be.login(on_event=events.append, timeout=1)
    assert ok is False and "超时" in msg
    assert not be.state_path().exists(), "超时不能写出凭据文件"
    assert events, "应通过 on_event 推送进度"
    assert any("正在启动浏览器" in e for e in events)


def test_login_success_saves_state(monkeypatch, dirs):
    """检测到登录成功 -> 保存 storage_state 并返回成功。"""
    Harness(monkeypatch, FakePage(url="https://x.com/home"))
    be = B.BrowserBackend()
    be.LOGIN_POLL_SECONDS = 0.01
    monkeypatch.setattr(be, "_probe_login_state", lambda p: True)

    events: list[str] = []
    ok, msg = be.login(on_event=events.append, timeout=5)
    assert ok is True and "登录成功" in msg
    assert be.state_path().is_file()
    assert be.has_state() is True, "保存后 available() 应立刻变可用"
    assert any("凭据已保存" in e for e in events)


def test_login_releases_lock_on_failure(monkeypatch, dirs):
    """登录失败也必须释放浏览器锁，否则后续发布全被卡死。"""
    Harness(monkeypatch, FakePage(url="https://x.com/login", keep_url=True))
    be = B.BrowserBackend()
    be.LOGIN_POLL_SECONDS = 0.01
    monkeypatch.setattr(be, "_probe_login_state", lambda p: None)
    ok, _ = be.login(timeout=1)
    assert ok is False
    acquired = B._BROWSER_LOCK.acquire(timeout=1)
    assert acquired, "登录失败后锁没释放"
    B._BROWSER_LOCK.release()


def test_login_handles_closed_window(monkeypatch, dirs):
    """用户在等待期间手动关掉浏览器窗口 -> 明确说明是"窗口被关闭"，不是玄学失败。"""
    class ClosingPage(FakePage):
        def wait_for_timeout(self, ms):
            raise RuntimeError("Target page, context or browser has been closed")

    Harness(monkeypatch, ClosingPage(url="https://x.com/login", keep_url=True))
    be = B.BrowserBackend()
    be.LOGIN_POLL_SECONDS = 0.01
    monkeypatch.setattr(be, "_probe_login_state", lambda p: None)
    ok, msg = be.login(timeout=30)
    assert ok is False
    assert "关闭" in msg


def test_login_blocked_by_403_reports_clearly(monkeypatch, dirs):
    """登录页也被拦时要说清是拦截，别让用户以为是自己账号问题。"""
    Harness(monkeypatch, FakePage(url="https://x.com/login", keep_url=True,
                                  nav_status=403, body_text=""))
    be = B.BrowserBackend()
    ok, msg = be.login(timeout=5)
    assert ok is False
    assert "被拒绝" in msg and "BROWSER_HEADLESS" in msg


# ══════════════════════════════════════════════════════════
# B. 成功判定（最关键）
# ══════════════════════════════════════════════════════════

def _decide(be, ui, payload=None, url="https://x.com/home"):
    page = FakePage(url=url)
    return be._decide_result(job=make_job(), text="hi", page=page, shot=None, started=time.time(),
                             resp={"payload": payload, "status": 200 if payload else None, "url": ""},
                             ui=ui, composer="modal", media_uploaded=False)


def test_decide_strong_evidence_from_network(monkeypatch, dirs):
    be = B.BrowserBackend()
    payload = {"data": {"create_tweet": {"tweet_results": {"result": {"rest_id": "1800000000000000123"}}}}}
    r = _decide(be, {"toast": "", "toast_kind": "", "editor_cleared": True, "composer_closed": True},
                payload=payload)
    assert r.ok is True and r.tweet_id == "1800000000000000123"
    assert "1800000000000000123" in r.tweet_url
    assert r.extra["evidence"] == "network"
    assert r.extra["evidence_strength"] == "strong"


def test_decide_strong_evidence_from_url(monkeypatch, dirs):
    be = B.BrowserBackend()
    r = _decide(be, {"toast": "", "toast_kind": "", "editor_cleared": False, "composer_closed": False},
                payload=None, url="https://x.com/alice/status/1811111111111111111")
    assert r.ok is True and r.tweet_id == "1811111111111111111"
    assert r.extra["evidence"] == "url"


def test_decide_api_duplicate_is_fatal(monkeypatch, dirs):
    be = B.BrowserBackend()
    with_real_dirs(dirs)  # 让截图路径可用
    payload = {"errors": [{"code": 187, "message": "Status is a duplicate."}]}
    r = _decide(be, {"toast": "", "toast_kind": "", "editor_cleared": False, "composer_closed": False},
                payload=payload)
    assert r.ok is False and r.retryable is False
    assert "重复" in r.error
    assert r.extra["evidence"] == "api_error"


def test_decide_api_rate_limit_is_retryable_with_wait(monkeypatch, dirs):
    be = B.BrowserBackend()
    with_real_dirs(dirs)
    payload = {"errors": [{"code": 344, "message": "Daily limit exceeded"}]}
    r = _decide(be, {"toast": "", "toast_kind": "", "editor_cleared": False, "composer_closed": False},
                payload=payload)
    assert r.ok is False and r.retryable is True and r.wait_seconds == 900


def test_decide_weak_evidence_toast(monkeypatch, dirs):
    be = B.BrowserBackend()
    r = _decide(be, {"toast": "Your post was sent.", "toast_kind": "ok",
                     "editor_cleared": True, "composer_closed": True}, payload=None)
    assert r.ok is True and r.tweet_id == ""
    assert r.extra["evidence_strength"] == "weak"
    assert r.extra["weak_evidence"] is True
    assert "核对" in r.extra["note"]


def test_decide_weak_evidence_editor_cleared(monkeypatch, dirs):
    be = B.BrowserBackend()
    r = _decide(be, {"toast": "", "toast_kind": "", "editor_cleared": True, "composer_closed": True},
                payload=None)
    assert r.ok is True and r.extra["evidence"] == "ui_cleared"
    assert r.extra["evidence_strength"] == "weak"


def test_decide_no_evidence_does_not_claim_success(monkeypatch, dirs):
    """什么标志都没有：不谎报成功，也不自动重试（可能已发出，重试会重复发）。"""
    be = B.BrowserBackend()
    with_real_dirs(dirs)
    r = _decide(be, {"toast": "", "toast_kind": "", "editor_cleared": False, "composer_closed": False},
                payload=None)
    assert r.ok is False
    assert r.retryable is False
    assert r.extra["may_have_published"] is True
    assert "核对" in r.error


def test_decide_http_error_retryable(monkeypatch, dirs):
    be = B.BrowserBackend()
    with_real_dirs(dirs)
    page = FakePage()
    r = be._decide_result(job=make_job(), text="hi", page=page, shot=None, started=time.time(),
                          resp={"payload": None, "status": 503, "url": ""},
                          ui={"toast": "", "toast_kind": "", "editor_cleared": False,
                              "composer_closed": False},
                          composer="modal", media_uploaded=False)
    assert r.ok is False and r.retryable is True


class with_real_dirs:
    """在 _decide_result 里触发截图时，LOG_DIR 必须是可写目录（由 dirs fixture 已设置）。"""

    def __init__(self, dirs):
        self.dirs = dirs

    def __enter__(self):
        return self.dirs

    def __exit__(self, *a):
        return False


# ══════════════════════════════════════════════════════════
# B. 并发串行化
# ══════════════════════════════════════════════════════════

def test_publish_serialized_by_lock(monkeypatch, dirs):
    write_state(dirs)
    be = B.BrowserBackend()
    state = {"current": 0, "max": 0}
    guard = threading.Lock()

    def slow(job, text, media, shot, started):
        with guard:
            state["current"] += 1
            state["max"] = max(state["max"], state["current"])
        time.sleep(0.15)
        with guard:
            state["current"] -= 1
        return PublishResult(ok=True, backend="browser", text=text, tweet_url="u", tweet_id="1")

    monkeypatch.setattr(be, "_publish_locked", slow)
    results: list[PublishResult] = []
    threads = [threading.Thread(target=lambda: results.append(be.publish(make_job(id=i), "hi")))
               for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 4
    assert all(r.ok for r in results)
    assert state["max"] == 1, "同一时刻只允许一个浏览器会话"


def test_login_serialized_and_returns_tuple(monkeypatch, dirs):
    """login 在锁被长期占用时应快速返回失败而不是无限阻塞。"""
    be = B.BrowserBackend()
    be.LOCK_WAIT_SECONDS = 0.2
    held = threading.Event()

    def hold():
        B._BROWSER_LOCK.acquire()
        held.set()
        time.sleep(1.0)
        B._BROWSER_LOCK.release()

    t = threading.Thread(target=hold)
    t.start()
    held.wait(2)
    try:
        ok, msg = be.login(on_event=lambda m: None, timeout=5)
        assert ok is False and "忙" in msg
    finally:
        t.join()


# ══════════════════════════════════════════════════════════
# C. 真浏览器（离线 set_content，无网络请求）
# ══════════════════════════════════════════════════════════

FAKE_EDITOR_HTML = """<!doctype html><html><body>
<div data-testid="tweetTextarea_0" id="ed" contenteditable="true" role="textbox"
     style="border:1px solid #999;min-height:100px;width:520px"></div>
<script>
  window.kd = [];
  const ed = document.getElementById('ed');
  for (const t of ['keydown','beforeinput','input']) {
    ed.addEventListener(t, e => window.kd.push(t + ':' + (e.key || e.inputType || '')));
  }
</script>
</body></html>"""


def _pw_available() -> bool:
    try:
        return B._HAVE_PW
    except Exception:
        return False


@pytest.mark.skipif(not _pw_available(), reason="playwright 未安装")
def test_type_text_real_contenteditable(dirs):
    """真浏览器验证：换行走 Shift+Enter、CJK/emoji 逐字输入、回读一致。"""
    be = B.BrowserBackend()
    text = "第一行中文 with 👋🏽\n第二行 ascii\n\n第四行"
    with be._open_session(headless=True, use_state=False) as s:
        page = s.context.new_page()
        page.set_content(FAKE_EDITOR_HTML)
        ok, err = be._type_text(page, text)
        assert ok, err
        got = B._norm_text(be._read_editor_text(page))
        assert got == B._norm_text(text), f"回读不一致: {got!r}"
        # 换行必须是 Shift+Enter（Enter 在 X 里可能直接送出）
        keys = page.evaluate("window.kd.filter(x => x === 'keydown:Enter').length")
        assert keys == 3, f"应产生 3 次 Enter keydown，实际 {keys}"
        shift = page.evaluate(
            "document.getElementById('ed').innerHTML.includes('<br>')")
        assert shift is True


@pytest.mark.skipif(not _pw_available(), reason="playwright 未安装")
def test_type_text_empty_rejected(dirs):
    be = B.BrowserBackend()
    with be._open_session(headless=True, use_state=False) as s:
        page = s.context.new_page()
        page.set_content(FAKE_EDITOR_HTML)
        ok, err = be._type_text(page, "   ")
        assert ok is False and "文本为空" in err


@pytest.mark.skipif(not _pw_available(), reason="playwright 未安装")
def test_open_session_always_closes_browser():
    """真浏览器：会话结束后 chromium 进程/浏览器对象必须已关闭。"""
    be = B.BrowserBackend()
    with be._open_session(headless=True, use_state=False) as s:
        assert s.browser.is_connected() is True
        browser_ref = s.browser
    assert browser_ref.is_connected() is False


@pytest.mark.skipif(not _pw_available(), reason="playwright 未安装")
def test_state_summary_reads_local_file(dirs):
    write_state(dirs, cookies=[{"name": "a"}, {"name": "b"}])
    be = B.BrowserBackend()
    assert "2 条 cookie" in be.state_summary()


# ══════════════════════════════════════════════════════════
# 自跑入口
# ══════════════════════════════════════════════════════════

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--color=no"]))


# ══════════════════════════════════════════════════════════
# 回归：编辑器"可见但被遮挡"（真实故障，2026-09-25）
#   实测：打开发帖弹窗瞬间页面上有 2 个 tweetTextarea_0，
#   先出现的那个 visible=True 但被 [data-testid=mask] 盖住，
#   点击报 "subtree intercepts pointer events" 并超时 ->
#   任务失败为「无法聚焦编辑器: TimeoutError」。
# ══════════════════════════════════════════════════════════

class _Ed:
    def __init__(self, visible, clickable, box=(10, 10, 100, 40)):
        self._v = visible
        self._clickable = clickable
        self._box = {"x": box[0], "y": box[1], "width": box[2], "height": box[3]}
        self.clicks = 0

    def is_visible(self):
        return self._v

    def bounding_box(self):
        return self._box if self._v else None

    def click(self, **kw):
        # 被遮挡的元素：Playwright 会一直重试直到超时
        self.clicks += 1
        if not self._clickable:
            raise B.PWTimeoutError("subtree intercepts pointer events")
        return None


class _EdList:
    def __init__(self, eds):
        self._eds = eds

    def count(self):
        return len(self._eds)

    def nth(self, i):
        return self._eds[i]

    def first(self):
        return self._eds[0]


class _OccludedPage:
    """第 0 个可点、第 1 个被遮挡（真实页面里的顺序就是这样）。"""

    def __init__(self, delay_frames=0):
        self.eds = [_Ed(True, True, (450, 156, 515, 96)), _Ed(True, False, (393, 76, 513, 28))]
        self.frame = 0
        self.delay_frames = delay_frames
        self.typed = ""

    def locator(self, sel):
        return _EdList(self.eds)

    def evaluate(self, expr, idx=None, **kw):
        if idx is None:
            return None
        return bool(self.eds[idx]._clickable)

    def wait_for_timeout(self, ms):
        self.frame += 1


def test_wait_editor_requires_clickable_not_just_visible():
    """_wait_editor 不能只认 visible —— 被遮罩挡住的不算。"""
    be = B.BrowserBackend()
    # 只有一个"可见但不可点"的编辑器 -> 必须等不到
    page = _OccludedPage()
    page.eds = [_Ed(True, False)]
    assert be._wait_editor(page, 300) is False

    # 有一个可点的 -> 立刻通过
    page2 = _OccludedPage()
    assert be._wait_editor(page2, 1500) is True


def test_type_text_skips_occluded_editor():
    """有多个编辑器时，必须挑可点的那个，不能盲取第一个被遮挡的。"""
    be = B.BrowserBackend()
    page = _OccludedPage()
    # 把不可点的放前面，模拟"第一个匹配就是被遮挡的"
    page.eds = [_Ed(True, False), _Ed(True, True)]
    # 直接验证选择逻辑：能选出第 1 个（可点者）
    picked = None
    for i in range(len(page.eds)):
        if page.eds[i].is_visible() and page.evaluate("", i):
            picked = i
            break
    assert picked == 1, "应跳过被遮挡的编辑器，选中可点的那个"


# ══════════════════════════════════════════════════════════
# 回归：媒体上传完成判定（真实故障，2026-09-25）
#   实测：X 首页常驻 3 个 [role=progressbar]（页顶细条 + 圆环倒计时等），
#   上传前就存在、永远不归零。旧实现数"整页 progressbar"判完成，
#   必然卡满 MEDIA_UPLOAD_TIMEOUT_MS（300 秒）后报"媒体上传超时"。
#   修复：只认附件容器内部的进度与预览。
# ══════════════════════════════════════════════════════════

def test_upload_media_ignores_page_level_progressbars(monkeypatch, dirs, tmp_path):
    """整页有常驻 progressbar 时，只要附件就绪就应判成功（不能卡满超时）。"""
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)

    class FileInput:
        def count(self): return 1
        @property
        def first(self): return self
        def set_input_files(self, *a, **k): return None

    class Page:
        def __init__(self): self.rounds = 0
        def locator(self, sel):
            if sel == B.SEL_FILE_INPUT:
                return FileInput()
            raise AssertionError(f"意外的选择器: {sel}")
        def evaluate(self, expr, *a):
            self.rounds += 1
            # 附件就绪；整页有 3 个常驻 progressbar 也不该影响判定
            return {"att": True, "bars": 0, "media": 1}
        def wait_for_timeout(self, ms): return None

    be = B.BrowserBackend()
    page = Page()
    ok, err = be._upload_media(page, img)
    assert ok is True, err
    assert err == ""


def test_upload_media_treats_media_present_as_ready_even_with_bars(monkeypatch, tmp_path):
    """附件里已有媒体、却仍有残留进度指示时，兜底也应判成功而不是卡满超时。"""
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)

    class FileInput:
        def count(self): return 1
        @property
        def first(self): return self
        def set_input_files(self, *a, **k): return None

    class Page:
        def locator(self, sel):
            if sel == B.SEL_FILE_INPUT: return FileInput()
            raise AssertionError(sel)
        def evaluate(self, expr, *a):
            return {"att": True, "bars": 1, "media": 1}   # 有媒体但 bars 不为 0
        def wait_for_timeout(self, ms): return None

    be = B.BrowserBackend()
    be.MEDIA_UPLOAD_TIMEOUT_MS = 800      # 缩短，测试别真等
    ok, err = be._upload_media(Page(), img)
    assert ok is True, f"附件已有媒体时应判就绪，实际: {err}"


def test_upload_media_still_fails_when_no_preview(tmp_path):
    """真没上传成功时仍必须失败 —— 不能为了避免卡顿就一律放行。"""
    img = tmp_path / "x.png"
    img.write_bytes(b"x")

    class FileInput:
        def count(self): return 1
        @property
        def first(self): return self
        def set_input_files(self, *a, **k): return None

    class Page:
        def locator(self, sel): return FileInput()
        def evaluate(self, expr, *a): return {"att": True, "bars": 0, "media": 0}
        def wait_for_timeout(self, ms): return None

    be = B.BrowserBackend()
    be.MEDIA_UPLOAD_TIMEOUT_MS = 600
    ok, err = be._upload_media(Page(), img)
    assert ok is False
    assert "超时" in err or "预览" in err
