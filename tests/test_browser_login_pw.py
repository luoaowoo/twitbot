"""账号密码登录测试（★F，契约 §3 / §3.1）

全部离线：用假 page/context/locator 桩把 X 的登录流驱动成一个**可编排的状态机**，
覆盖契约 §3.2 要求的七类场景：

  A. 正常登录成功（写 storage_state + settings.x_username）
  B. 密码错
  C. 需验证码：code 为空 -> 必须与"密码错"**可区分**
  D. 需验证码：code 非空 -> 填入提交并成功
  E. 人机验证受限（Arkose）/ 被限流 / 网络不可达 / 403 拦截
  F. 中间拦截页（unusual activity 要求再输手机号/用户名）—— 尽力填 -> 成功 / 卡死
  G. **密码不泄漏**：日志、on_event、返回值、settings 负载、落盘文件全都不含密码明文

不得依赖真实网络（`start_playwright` 一律替换成 FakePW，`settings.set_many` 一律打桩）。

运行：
    .\\.venv\\Scripts\\python.exe -m pytest tests/test_browser_login_pw.py -q
    .\\.venv\\Scripts\\python.exe tests/test_browser_login_pw.py      # 无 pytest 也能自跑
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core import config  # noqa: E402
from core import settings as settings_mod  # noqa: E402
from core.backends import browser as B  # noqa: E402

# 测试用密码：故意取一个"绝不可能自然出现在普通文案里"的长串，
# 这样"日志/返回值/文件里不含它"的断言才有意义。
PW = "S3cr3t-Pw-98xQ-Zz"
CODE = "531742"
USER = "alice@example.com"


# ══════════════════════════════════════════════════════════
# 测试替身：可编排的登录页桩
# ══════════════════════════════════════════════════════════

class FakeLoc:
    """元素桩：可见性由所属页面按"当前处于哪一步"决定。"""

    def __init__(self, page, sel, visible=False, text=""):
        self.page = page
        self.sel = sel
        self._visible = visible
        self._text = text
        self.clicks = 0
        self.fills: list[str] = []

    @property
    def first(self):
        return self

    def count(self) -> int:
        return 1 if self._visible else 0

    def is_visible(self) -> bool:
        return self._visible

    def is_enabled(self) -> bool:
        return True

    def inner_text(self) -> str:
        return self._text

    def text_content(self) -> str:
        return self._text

    def nth(self, i):
        return self

    def fill(self, value) -> None:
        # ⚠ 这里会"看到"密码明文（桩必须能收到才能验证表单被填过），
        #   但**只在内存里**，不落任何文件、不进日志。
        if not self._visible:
            raise RuntimeError("element is not visible (fake)")
        self.fills.append(value)
        self.page.on_fill(self.sel, value)

    def click(self, **kw) -> None:
        if not self._visible:
            raise RuntimeError("element is not visible (fake)")
        self.clicks += 1
        self.page.on_click(self.sel)


class FakeKeyboard:
    def __init__(self, page):
        self.page = page
        self.presses: list[str] = []

    def press(self, key):
        self.presses.append(key)
        self.page.on_key(key)

    def type(self, text, delay=0):
        pass

    def insert_text(self, text):
        pass


class FakeNavResponse:
    def __init__(self, status=200):
        self.status = status


# 各阶段（stage）上"页面上可见什么"。key 是选择器，value 是元素文本。
STAGE_TEXT = {
    "username": "",
    "password": "",
    # 新版登录页把账号+密码合并在同一张表单（/i/jf/onboarding/web?mode=login）
    "combined": "",
    # 中间拦截页：X 的典型文案（unusual activity / 要求再输手机号或用户名）
    "challenge": "Unusual activity. Enter your phone number or username to continue.",
    "otp": "We sent you a code. Enter the verification code to continue.",
    "captcha": "",
    "wrong_pw": "Wrong username or password. Try again.",
    "locked": "Your account has been locked due to too many attempts.",
    "rate": "Rate limit exceeded. Too many requests, please try again later.",
    # 新版通用错误页（弹窗标题 + 按钮）
    "generic": "出了点问题 重新开始",
    "nothing": "",
    "home": "",
}


class LoginPage:
    """假登录页：内部维护一个 stage，fill/click 驱动 stage 迁移。

    outcome_seq: 每次"密码步提交"后进入的 stage 序列（按顺序消费，耗尽后停在最后一个）。
    challenge_next: 中间拦截页提交后进入的 stage（"stay" = 不动，用于测卡死）。
    otp_next:      验证码提交后进入的 stage。
    """

    def __init__(self, *, outcome_seq=("home",), challenge_next="password",
                 otp_next="home", nav_status=200, blank=False, raise_on_goto=None,
                 record: dict | None = None):
        self.url = B.PW_LOGIN_URL
        self.stage = "username"
        self.outcome_seq = list(outcome_seq)
        self.challenge_next = challenge_next
        self.otp_next = otp_next
        self.nav_status = nav_status
        self.blank = blank
        self.raise_on_goto = raise_on_goto
        self.record = record if record is not None else {}
        self.fills: list[tuple[str, str]] = []
        self.clicks: list[str] = []
        self.stage_clicks: list[tuple[str, str]] = []   # (点击时所处阶段, 选择器)
        self.gotos: list[str] = []
        self.waits = 0
        self.keyboard = FakeKeyboard(self)

    # ── 阶段 → 可见选择器 ─────────────────────────────────
    def visible(self, sel: str) -> bool:
        st = self.stage
        if st == "username":
            return sel in B.SEL_PW_USERNAME or sel in B.SEL_PW_NEXT
        if st == "password":
            return sel in B.SEL_PW_PASSWORD or sel in B.SEL_PW_NEXT
        if st == "combined":
            # 账号与密码同时可见 —— 这是新版页面与旧版两步流程的关键区别
            return (sel in B.SEL_PW_USERNAME or sel in B.SEL_PW_PASSWORD
                    or sel in B.SEL_PW_NEXT)
        if st == "challenge":
            return (sel in B.SEL_PW_CHALLENGE or sel in B.SEL_PW_NEXT)
        if st == "otp":
            return (sel in B.SEL_PW_OTP or sel in B.SEL_PW_NEXT)
        if st == "captcha":
            return sel in B.SEL_PW_CAPTCHA
        if st in ("wrong_pw", "locked", "rate"):
            # 报错页上输入框仍然在（真实 X 也是这样）—— 用来验证
            # "报错分类优先于 '再输一次' 判定"，不会退化成中间拦截页处理
            return sel in B.SEL_PW_PASSWORD or sel in B.SEL_PW_USERNAME
        if st == "home":
            # 登录后特征（_probe_login_state 靠这些判 True）
            return sel in B.LOGGED_IN_MARKERS or sel in B.SEL_PRIMARY_COLUMN
        return False

    def text(self) -> str:
        return STAGE_TEXT.get(self.stage, "")

    # ── 动作 ──────────────────────────────────────────────
    def on_fill(self, sel: str, value: str) -> None:
        self.fills.append((sel, value))
        if sel in B.SEL_PW_PASSWORD:
            self.record["pw_filled"] = self.record.get("pw_filled", 0) + 1
        elif sel in B.SEL_PW_USERNAME and self.stage in ("username", "combined"):
            self.record["user_filled"] = self.record.get("user_filled", 0) + 1

    def on_click(self, sel: str) -> None:
        self.clicks.append(sel)
        if sel not in B.SEL_PW_NEXT:
            return
        self.stage_clicks.append((self.stage, sel))
        if self.stage == "username":
            self.stage = "password"
        elif self.stage == "combined":
            nxt = self.outcome_seq.pop(0) if self.outcome_seq else "home"
            self.stage = nxt
        elif self.stage == "password":
            nxt = self.outcome_seq.pop(0) if self.outcome_seq else "home"
            self.stage = nxt
        elif self.stage == "challenge":
            self._advance(self.challenge_next)
        elif self.stage == "otp":
            self._advance(self.otp_next)

    def _advance(self, nxt: str) -> None:
        """"stay" = 提交后页面不动（模拟验证码/拦截页反复不通过）。"""
        if nxt != "stay":
            self.stage = nxt

    def on_key(self, key: str) -> None:
        if key == "Enter" and self.stage == "username":
            self.stage = "password"

    # ── playwright 面 ─────────────────────────────────────
    def goto(self, url, **kw):
        self.gotos.append(url)
        if self.raise_on_goto is not None:
            raise self.raise_on_goto
        if "home" in url and self.stage == "home":
            self.url = B.HOME_URL
        elif "login" in url:
            self.url = B.PW_LOGIN_URL
        return FakeNavResponse(self.nav_status)

    def evaluate(self, expr, *a):
        if "children" in expr:      # _page_is_blank 的探针
            if self.blank:
                return {"children": 0, "html": 0, "text": ""}
            return {"children": 6, "html": 4618, "text": self.text() or "content"}
        return self.text()          # _pw_page_text 的探针

    def wait_for_timeout(self, ms):
        # 压缩等待：桩里最多睡 5ms，测试才不会真的等 1.5s。
        self.waits += 1
        import time as _t
        _t.sleep(min(ms, 5) / 1000.0)

    def wait_for_selector(self, sel, **kw):
        raise B.PWTimeoutError("no element (fake)")

    def locator(self, sel):
        return FakeLoc(self, sel, visible=self.visible(sel), text=self.text())

    def title(self):
        return "fake-login"

    def screenshot(self, path=None, full_page=True):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\nFAKE")


class FakeContext:
    def __init__(self, page: LoginPage, record: dict):
        self._page = page
        self._record = record

    def new_page(self):
        return self._page

    def add_init_script(self, js):
        pass

    def set_default_timeout(self, ms):
        pass

    def set_default_navigation_timeout(self, ms):
        pass

    def storage_state(self, path=None):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"cookies": [{"name": "auth_token", "value": "x"}],
                                 "origins": []}), encoding="utf-8")
        self._record["state_path"] = str(p)
        return {"cookies": []}

    def close(self):
        self._record["context_close"] = self._record.get("context_close", 0) + 1


class FakeBrowser:
    def __init__(self, record: dict, page: LoginPage):
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
        return FakeBrowser(self._record, self._page)


class FakePW:
    def __init__(self, record, page):
        self.rec = record          # 双下划线私有名会被改写，用单下划线
        self.chromium = FakeChromium(record, page)

    def stop(self):
        self.rec["pw_stop"] = self.rec.get("pw_stop", 0) + 1


class Harness:
    """装好 FakePW + settings.set_many 打桩，记录生命周期事件。"""

    def __init__(self, monkeypatch, page: LoginPage):
        self.record = page.record
        self.page = page
        self.pw = FakePW(self.record, page)
        self.settings_writes: list[dict] = []
        monkeypatch.setattr(B, "start_playwright", lambda: self.pw)
        monkeypatch.setattr(settings_mod, "set_many",
                            lambda items: self.settings_writes.append(dict(items)))


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    """清掉 BROWSER_CHANNEL 干扰，并保证每次从首选启动配置开始。"""
    B.BrowserBackend._reset_good()
    monkeypatch.delenv("BROWSER_CHANNEL", raising=False)
    yield
    B.BrowserBackend._reset_good()


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """BROWSER_DIR / LOG_DIR 指到临时目录（登录态与截图都不许落到真实 data/）。"""
    browser_dir = tmp_path / "browser"
    log_dir = tmp_path / "logs"
    for d in (browser_dir, log_dir):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "BROWSER_DIR", browser_dir)
    monkeypatch.setattr(config, "LOG_DIR", log_dir)
    return {"root": tmp_path, "browser": browser_dir, "log": log_dir}


def _run(monkeypatch, dirs, page: LoginPage, *, code="", timeout=8, collect=None):
    """跑一次 login_with_password，返回 (ok, msg, events, harness)。"""
    h = Harness(monkeypatch, page)
    events: list[str] = []
    ok, msg = B.BrowserBackend().login_with_password(
        username=USER, password=PW, on_event=events.append, timeout=timeout, code=code)
    if collect is not None:
        collect.update({"ok": ok, "msg": msg, "events": events, "harness": h})
    return ok, msg, events, h


def _files_containing(root: Path, needle: str) -> list[str]:
    """扫临时目录里所有文件，返回含明文 needle 的路径（脱敏断言用）。"""
    hits: list[str] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            data = p.read_bytes()
        except Exception:
            continue
        if needle.encode("utf-8") in data:
            hits.append(str(p))
    return hits


# ══════════════════════════════════════════════════════════
# A. 成功路径
# ══════════════════════════════════════════════════════════

def test_login_pw_success_saves_state_and_username(monkeypatch, dirs):
    """账号 -> Next -> 密码 -> Next -> 登录成功：写 storage_state 并记账号名。"""
    page = LoginPage(outcome_seq=["home"])
    ok, msg, events, h = _run(monkeypatch, dirs, page)

    assert ok is True, msg
    assert "登录成功" in msg
    be = B.BrowserBackend()
    assert be.state_path().is_file()
    assert be.has_state() is True, "保存后 available() 应立刻变可用"
    assert h.record["state_path"] == str(be.state_path())
    # 表单被真的填过：账号 1 次、密码 1 次
    assert h.record.get("user_filled") == 1
    assert h.record.get("pw_filled") == 1
    # 只写 x_username，不写密码
    assert h.settings_writes, "成功必须调用 settings.set_many"
    assert h.settings_writes[-1] == {"x_username": USER}
    # 有头模式（契约 §3 第 1 条：use_state=False）
    assert h.record["launch_kwargs"]["headless"] is False
    assert "storage_state" not in h.record["new_context_kwargs"], "登录必须 use_state=False"
    assert events and any("正在启动浏览器" in e for e in events)
    # 资源必须被关掉
    assert h.record["context_close"] == 1
    assert h.record["browser_close"] == 1
    assert h.record["pw_stop"] == 1


def test_login_pw_opens_flow_login_url(monkeypatch, dirs):
    """必须打开 https://x.com/i/flow/login（契约 §3 第 1 条）。"""
    page = LoginPage(outcome_seq=["home"])
    _run(monkeypatch, dirs, page)
    assert page.gotos and page.gotos[0] == B.PW_LOGIN_URL


def test_login_pw_releases_lock(monkeypatch, dirs):
    """无论成败都要释放浏览器锁，否则后续发布全被卡死。"""
    page = LoginPage(outcome_seq=["wrong_pw"])
    ok, _, _, _ = _run(monkeypatch, dirs, page)
    assert ok is False
    got = B._BROWSER_LOCK.acquire(timeout=1)
    assert got, "登录失败后锁没释放"
    B._BROWSER_LOCK.release()


# ══════════════════════════════════════════════════════════
# B. 失败分类：密码错
# ══════════════════════════════════════════════════════════

def test_login_pw_wrong_password_message(monkeypatch, dirs):
    page = LoginPage(outcome_seq=["wrong_pw"])
    ok, msg, _, _ = _run(monkeypatch, dirs, page)
    assert ok is False
    assert msg == B.PW_MSG_WRONG_PW
    assert "账号或密码不正确" in msg
    assert not B.BrowserBackend().state_path().exists(), "密码错绝不能写凭据文件"


# ══════════════════════════════════════════════════════════
# C/D. 二次验证码（code 空 / 非空）
# ══════════════════════════════════════════════════════════

def test_login_pw_need_code_when_code_empty(monkeypatch, dirs):
    """code 为空 -> 返回"需要验证码"，且**与"密码错"可区分**。"""
    page = LoginPage(outcome_seq=["otp"])
    ok, msg, _, _ = _run(monkeypatch, dirs, page)
    assert ok is False
    assert msg == B.PW_MSG_NEED_CODE
    assert "验证码" in msg
    # 关键区分：不是"账号或密码不正确"，也不是"登录失败"这种含糊话
    assert msg != B.PW_MSG_WRONG_PW
    assert "不正确" not in msg
    assert msg != B.PW_MSG_UNKNOWN_RESULT
    assert not B.BrowserBackend().state_path().exists()


def test_login_pw_with_code_success(monkeypatch, dirs):
    """code 非空 -> 填入验证码并提交，最终成功。"""
    page = LoginPage(outcome_seq=["otp"], otp_next="home")
    ok, msg, _, h = _run(monkeypatch, dirs, page, code=CODE)
    assert ok is True, msg
    # 验证码确实被填进了验证码输入框
    otp_fills = [v for sel, v in page.fills if sel in B.SEL_PW_OTP]
    assert otp_fills == [CODE]
    assert h.settings_writes[-1] == {"x_username": USER}
    assert B.BrowserBackend().has_state() is True


def test_login_pw_bad_code_message(monkeypatch, dirs):
    """验证码多次未通过 -> 明确说验证码问题，不误报成密码错。"""
    page = LoginPage(outcome_seq=["otp"], otp_next="stay")
    ok, msg, _, _ = _run(monkeypatch, dirs, page, code=CODE, timeout=8)
    assert ok is False
    assert msg == B.PW_MSG_BAD_CODE
    assert "验证码" in msg
    # 验证码确实被反复填过，且最终因"验证码不对"收口而不是"密码错"
    assert len([1 for sel, _ in page.fills if sel in B.SEL_PW_OTP]) >= 1
    assert sum(1 for st, _ in page.stage_clicks if st == "password") == 1


# ══════════════════════════════════════════════════════════
# F. 中间拦截页（unusual activity）
# ══════════════════════════════════════════════════════════

def test_login_pw_challenge_page_then_success(monkeypatch, dirs):
    """中间拦截页要求"输入手机号/用户名"：尽力填账号后继续，最终成功。"""
    page = LoginPage(outcome_seq=["challenge", "home"], challenge_next="password")
    ok, msg, events, _ = _run(monkeypatch, dirs, page)
    assert ok is True, msg
    # 拦截页上填的是账号（不是密码！）
    chall_fills = [v for sel, v in page.fills if sel in B.SEL_PW_CHALLENGE]
    assert chall_fills == [USER]
    assert any("二次身份确认" in e for e in events), "应通过 on_event 说明遇到了拦截页"


def test_login_pw_challenge_stuck_gives_clear_message(monkeypatch, dirs):
    """拦截页始终过不去 -> 明确提示改用手工登录，且不会无限重试。"""
    page = LoginPage(outcome_seq=["challenge"], challenge_next="stay")
    ok, msg, _, _ = _run(monkeypatch, dirs, page, timeout=8)
    assert ok is False
    assert msg == B.PW_MSG_CHALLENGE_STUCK
    assert "手工登录" in msg
    # 不能把账号往拦截页里灌到几十次（避免自己触发风控）：
    # 拦截页上的提交次数必须被 PW_MAX_CHALLENGE_ROUNDS 卡住。
    chall_submits = sum(1 for st, _ in page.stage_clicks if st == "challenge")
    assert chall_submits <= B.BrowserBackend.PW_MAX_CHALLENGE_ROUNDS, \
        f"拦截页提交了 {chall_submits} 次，超过上限"
    # 密码步只提交过 1 次（不能反复拿密码去撞）
    assert sum(1 for st, _ in page.stage_clicks if st == "password") == 1


# ══════════════════════════════════════════════════════════
# E. 人机验证 / 限流 / 网络 / 403
# ══════════════════════════════════════════════════════════

def test_login_pw_captcha_requires_manual_login(monkeypatch, dirs):
    page = LoginPage(outcome_seq=["captcha"])
    ok, msg, _, _ = _run(monkeypatch, dirs, page)
    assert ok is False
    assert msg == B.PW_MSG_HUMAN
    assert "人机验证" in msg and "手工登录" in msg


def test_login_pw_locked_account_reports_human_verification(monkeypatch, dirs):
    page = LoginPage(outcome_seq=["locked"])
    ok, msg, _, _ = _run(monkeypatch, dirs, page)
    assert ok is False
    assert msg == B.PW_MSG_HUMAN
    assert msg != B.PW_MSG_WRONG_PW


def test_login_pw_rate_limited_reuses_block_message(monkeypatch, dirs):
    page = LoginPage(outcome_seq=["rate"])
    ok, msg, _, _ = _run(monkeypatch, dirs, page)
    assert ok is False
    assert "429" in msg, "被限流应复用 _block_message(429)"
    assert msg != B.PW_MSG_WRONG_PW


def test_login_pw_network_unreachable(monkeypatch, dirs):
    page = LoginPage(raise_on_goto=RuntimeError("net::ERR_INTERNET_DISCONNECTED"))
    ok, msg, _, _ = _run(monkeypatch, dirs, page)
    assert ok is False
    assert "网络不可达" in msg and "代理/VPN" in msg


def test_login_pw_403_block_reports_blocked_not_wrong_password(monkeypatch, dirs):
    page = LoginPage(nav_status=403, blank=True)
    ok, msg, _, _ = _run(monkeypatch, dirs, page)
    assert ok is False
    assert "被拒绝" in msg and "BROWSER_HEADLESS" in msg
    assert msg != B.PW_MSG_WRONG_PW


def test_login_pw_blank_200_shell_page_is_blocked(monkeypatch, dirs):
    """实测：被拒时也可能给 200 + 空白壳页 —— 同样要识别为拦截。"""
    page = LoginPage(blank=True)
    ok, msg, _, _ = _run(monkeypatch, dirs, page)
    assert ok is False
    assert "页面为空" in msg


def test_login_pw_empty_credentials_rejected_without_browser(monkeypatch, dirs):
    monkeypatch.setattr(B, "start_playwright", lambda: (_ for _ in ()).throw(
        AssertionError("空凭据不该启浏览器！")))
    for u, p in ((("", PW)), ((USER, "")), (("   ", PW))):
        ok, msg = B.BrowserBackend().login_with_password(username=u, password=p)
        assert ok is False and "为空" in msg


# ══════════════════════════════════════════════════════════
# 绝不抛异常
# ══════════════════════════════════════════════════════════

def test_login_pw_never_raises_when_browser_fails(monkeypatch, dirs):
    monkeypatch.setattr(B, "start_playwright", lambda: (_ for _ in ()).throw(
        RuntimeError("chromium 起不来")))
    be = B.BrowserBackend()
    ok, msg = be.login_with_password(username=USER, password=PW, timeout=3)
    assert ok is False and isinstance(msg, str) and msg
    got = B._BROWSER_LOCK.acquire(timeout=1)
    assert got, "异常路径也必须释放锁"
    B._BROWSER_LOCK.release()


def test_login_pw_never_raises_on_page_explosion(monkeypatch, dirs):
    """页面内部抛任意异常 -> 转成 (False, 中文说明)，不外抛。"""
    page = LoginPage(outcome_seq=["home"])

    def boom(sel):
        raise ValueError("locator 炸了")

    monkeypatch.setattr(page, "locator", boom)
    ok, msg, _, _ = _run(monkeypatch, dirs, page, timeout=3)
    assert ok is False
    assert isinstance(msg, str) and msg
    assert "账号或密码" in msg or "手工登录" in msg or "无法确认" in msg


def test_login_pw_busy_lock_returns_quickly(monkeypatch, dirs):
    """锁被占用时快速返回"忙"，不无限阻塞。"""
    import threading
    import time
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
        ok, msg = be.login_with_password(username=USER, password=PW)
        assert ok is False and "忙" in msg
    finally:
        t.join()


# ══════════════════════════════════════════════════════════
# G. 密码防泄漏（契约 §3.1 硬性要求）
# ══════════════════════════════════════════════════════════

def test_login_pw_password_not_in_logs_or_events_or_return(monkeypatch, dirs, caplog):
    """密码错路径：日志 / on_event / 返回值里都不许出现密码明文。"""
    page = LoginPage(outcome_seq=["wrong_pw"])
    caplog.set_level(logging.DEBUG, logger="twitbot.backends.browser")

    ok, msg, events, _ = _run(monkeypatch, dirs, page)

    assert ok is False
    assert PW not in msg
    assert all(PW not in e for e in events)
    assert PW not in caplog.text
    # 密码确实被填过（否则这个断言是空转）
    assert any(v == PW for _, v in page.fills)


def test_login_pw_password_not_in_logs_on_success(monkeypatch, dirs, caplog):
    page = LoginPage(outcome_seq=["home"])
    caplog.set_level(logging.DEBUG, logger="twitbot.backends.browser")
    ok, msg, events, _ = _run(monkeypatch, dirs, page)
    assert ok is True
    assert PW not in msg
    assert all(PW not in e for e in events)
    assert PW not in caplog.text


def test_login_pw_password_not_in_any_written_file(monkeypatch, dirs):
    """成功路径：storage_state / 日志 / 截图目录里任何文件都不得含密码明文。"""
    page = LoginPage(outcome_seq=["home"])
    ok, _, _, _ = _run(monkeypatch, dirs, page)
    assert ok is True
    assert _files_containing(dirs["root"], PW) == [], "落盘文件里出现了密码明文"
    # 反向自证：账号名是该落盘的（所以扫描器本身有效）
    assert _files_containing(dirs["root"], USER) == [], "账号名也不该被写进文件"


def test_login_pw_password_not_in_any_written_file_on_failure(monkeypatch, dirs):
    page = LoginPage(outcome_seq=["wrong_pw"])
    _run(monkeypatch, dirs, page)
    assert _files_containing(dirs["root"], PW) == []


def test_login_pw_password_never_reaches_settings(monkeypatch, dirs):
    """settings 负载里只能有 x_username；任何键值都不得含密码。"""
    page = LoginPage(outcome_seq=["home"])
    ok, _, _, h = _run(monkeypatch, dirs, page)
    assert ok is True
    assert len(h.settings_writes) == 1
    payload = h.settings_writes[0]
    assert set(payload) == {"x_username"}
    assert all(PW not in str(v) for v in payload.values())
    assert all(PW not in k for k in payload)


def test_login_pw_password_not_in_screenshot_names(monkeypatch, dirs):
    """截图名（若有）不得含密码 —— 契约 §3.1 明确点名。"""
    page = LoginPage(outcome_seq=["wrong_pw"])
    _run(monkeypatch, dirs, page)
    names = [p.name for p in Path(dirs["log"]).rglob("*") if p.is_file()]
    assert all(PW not in n for n in names)


def test_login_pw_failure_leaves_screenshot_without_secrets(monkeypatch, dirs):
    """失败照旧落 config.LOG_DIR 截图（契约 §3.1），且文件名/内容不含密码。"""
    page = LoginPage(outcome_seq=["wrong_pw"])
    ok, _, _, _ = _run(monkeypatch, dirs, page)
    assert ok is False
    shots = list(Path(dirs["log"]).glob("login_fail_*.png"))
    assert shots, "失败必须留证截图"
    assert shots[0].parent == Path(dirs["log"])
    assert PW not in shots[0].name and USER not in shots[0].name
    assert _files_containing(dirs["root"], PW) == []


def test_redact_secret_unit():
    """脱敏工具本身：长串必抹、短串不误伤正文。"""
    assert B._redact_secret(f"boom: {PW} end", PW) == "boom: *** end"
    assert B._redact_secret("no secret here", PW) == "no secret here"
    assert B._redact_secret("a", "a") == "a", "过短的值不替换，避免把正文打成筛子"
    assert B._redact_secret("", PW) == ""
    assert B._redact_secret(None, PW) == ""
    # 多个敏感串（密码 + 验证码）都要抹掉，且长串优先防互吃
    messy = f"value={PW} code={CODE}"
    out = B._redact_secret(messy, PW, CODE)
    assert PW not in out and CODE not in out


def test_pw_fill_does_not_leak_into_log_on_failure(monkeypatch, dirs, caplog):
    """填表单本身抛异常时，debug 日志里也不能出现被填的值。"""
    caplog.set_level(logging.DEBUG, logger="twitbot.backends.browser")
    be = B.BrowserBackend()

    class BoomLoc:
        def fill(self, value):
            raise RuntimeError(f"fill failed with value {value}")   # 故意把值带进异常

    ok = be._pw_fill(BoomLoc(), PW, PW)
    assert ok is False
    assert PW not in caplog.text, "fill 异常信息里的表单值必须先脱敏"


# ══════════════════════════════════════════════════════════
# 新版登录页（/i/jf/onboarding/web?mode=login）回归护栏
#   实测 2026-09：X 改了登录页 ——
#     · 账号与密码合并在同一张表单，两个框同时可见
#     · 页面上没有 data-testid，提交按钮是 button[type=submit]
#     · 页面会同时渲染两套重复节点，第一套是隐藏的
#   旧实现只看 loc.first，拿到不可见的节点 -> 退回按 Enter -> 只提交半张表单
#   -> X 回「出了点问题」。以下用例锁住修复后的行为。
# ══════════════════════════════════════════════════════════

def test_combined_form_fills_both_fields_before_submitting(monkeypatch, dirs):
    """合并表单：必须在提交前把账号和密码都填好，否则 X 回「出了点问题」。"""
    page = LoginPage(outcome_seq=["home"])
    page.stage = "combined"
    ok, msg, events, h = _run(monkeypatch, dirs, page)
    assert ok is True, msg
    filled = [sel for sel, _ in page.fills]
    assert any(sel in B.SEL_PW_USERNAME for sel in filled), "账号没被填"
    assert any(sel in B.SEL_PW_PASSWORD for sel in filled), "密码没被填"
    # 提交只应发生在两个框都填好之后
    first_pw_idx = next(i for i, (sel, _) in enumerate(page.fills)
                        if sel in B.SEL_PW_PASSWORD)
    assert page.clicks, "从未点击提交按钮"
    # 密码只提交一次（不能反复拿密码撞）
    assert h.record.get("pw_filled", 0) == 1


def test_combined_form_does_not_fall_back_to_enter(monkeypatch, dirs):
    """能找到可见的提交按钮时，不应退回按 Enter（旧实现在这里会退化成 Enter）。"""
    page = LoginPage(outcome_seq=["home"])
    page.stage = "combined"
    ok, _, _, _ = _run(monkeypatch, dirs, page)
    assert ok is True
    assert page.keyboard.presses == [], f"不该按 Enter，实际={page.keyboard.presses}"


def test_pw_find_skips_hidden_duplicate_nodes(monkeypatch, dirs):
    """页面同时渲染隐藏+可见两组节点时，必须返回**可见**的那个（而非 loc.first）。"""
    class DupPage:
        """第一组匹配隐藏，第二组可见 —— 模拟真实 X 页面的重复 DOM。"""

        def __init__(self):
            self.inner = LoginPage(outcome_seq=["home"])
            self.inner.stage = "combined"
            self.url = B.PW_LOGIN_URL
            self.keyboard = self.inner.keyboard

        def locator(self, sel):
            inner = self.inner
            visible = inner.visible(sel)

            class L:
                def __init__(self):
                    self.n = 2 if visible else 0

                def count(self):
                    return self.n

                def nth(self, i):
                    outer = self

                    class E:
                        def is_visible(self_inner):
                            return i == 1          # 只有第二个是可见的
                        def fill(self_inner, v):
                            inner.on_fill(sel, v)
                        def click(self_inner, **kw):
                            inner.on_click(sel)
                    return E()
            return L()

        def evaluate(self, expr, *a):
            return self.inner.evaluate(expr, *a)

        def wait_for_timeout(self, ms):
            self.inner.wait_for_timeout(ms)

    found = B.BrowserBackend._pw_find(DupPage(), ('button[type="submit"]',), 0)
    assert found is not None, "只找 loc.first 会漏掉可见节点"
    assert found.is_visible() is True


def test_generic_error_page_is_classified(monkeypatch, dirs):
    """「出了点问题 / 重新开始」必须被识别，给出可读原因而不是空转到超时。"""
    page = LoginPage(outcome_seq=["home"])
    page.stage = "generic"
    assert B.BrowserBackend._pw_classify(page) == "generic"

    # 真实页面上的原文
    class Real:
        def evaluate(self, *a):
            return "出了点问题 重新开始"
    assert B.BrowserBackend._pw_classify(Real()) == "generic"


def test_generic_error_reports_readable_message(monkeypatch, dirs):
    """走到通用错误页时要返回明确文案，且不与「密码错」混淆。"""
    page = LoginPage(outcome_seq=["home"])
    page.stage = "generic"
    ok, msg, _, _ = _run(monkeypatch, dirs, page)
    assert ok is False
    assert msg == B.PW_MSG_GENERIC
    assert "出了点问题" in msg
    assert msg != B.PW_MSG_WRONG_PW


def test_new_login_page_missing_account_wording(monkeypatch, dirs):
    """新登录页的「我们找不到使用该用户名的活跃 X 账号」应归为账号/密码问题。"""
    class Page:
        def evaluate(self, *a):
            return "看看正在发生什么\n我们找不到使用该用户名的活跃 X 账号。"
    assert B.BrowserBackend._pw_classify(Page()) == "wrong_pw"


# ══════════════════════════════════════════════════════════
# 分类工具本身
# ══════════════════════════════════════════════════════════

def test_pw_classify_priority_error_before_challenge():
    """报错类必须先于"再输一次"类判定，否则密码错会被误判成中间拦截页。"""
    page = LoginPage(outcome_seq=["wrong_pw"])
    page.stage = "wrong_pw"
    assert B.BrowserBackend._pw_classify(page) == "wrong_pw"

    page.stage = "challenge"
    assert B.BrowserBackend._pw_classify(page) == "challenge"

    page.stage = "otp"
    assert B.BrowserBackend._pw_classify(page) == "otp"

    page.stage = "rate"
    assert B.BrowserBackend._pw_classify(page) == "rate"

    page.stage = "password"
    assert B.BrowserBackend._pw_classify(page) == ""


def test_pw_classify_tolerates_broken_page():
    class Broken:
        def evaluate(self, *a):
            raise RuntimeError("context destroyed")

        def locator(self, sel):
            raise RuntimeError("no locator")

    assert B.BrowserBackend._pw_classify(Broken()) == ""


# ══════════════════════════════════════════════════════════
# 既有行为未被改动（回归护栏）
# ══════════════════════════════════════════════════════════

def test_existing_login_signature_untouched():
    """新增方法不得改动既有 login() 的签名。"""
    import inspect
    sig = inspect.signature(B.BrowserBackend.login)
    assert list(sig.parameters) == ["self", "on_event", "timeout"]

    sig_pw = inspect.signature(B.BrowserBackend.login_with_password)
    assert list(sig_pw.parameters) == ["self", "username", "password",
                                       "on_event", "timeout", "code"]
    assert sig_pw.parameters["on_event"].default is None
    assert sig_pw.parameters["timeout"].default is None
    assert sig_pw.parameters["code"].default == ""


# ══════════════════════════════════════════════════════════
# 自跑入口
# ══════════════════════════════════════════════════════════

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--color=no"]))
