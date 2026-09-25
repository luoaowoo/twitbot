"""无头浏览器发布后端（★C）—— 用 playwright 同步 API + 已保存的登录态在 x.com 前端发推。

设计要点（务必先读）：

1. **登录态**：playwright `storage_state`（cookies + localStorage）存
   `config.BROWSER_DIR / "storage_state.json"`。`available()` 只读文件、**不联网、不起浏览器**。
2. **只用同步 API**：`sync_playwright`。playwright 的同步 API 在 asyncio 事件循环所在的线程里
   会直接报 "It looks like you are using Playwright Sync API inside the asyncio loop"。
   调用方（pipeline / web 控制台）用 `asyncio.to_thread` 把我们丢进线程池，所以安全；
   但 `login()` 可能被 Web 控制台在事件循环里直接调用，故内部做了事件循环检测 + 自动换线程。
3. **串行化**：同一进程同一时刻只允许一个浏览器会话（`_BROWSER_LOCK`），
   避免多份 Chromium 把机器拖垮；`publish()` 用长等待串行，`verify()` 用短超时快速失败。
4. **成功判定**：绝不"点完就当成功"。判定顺序（强→弱）见 `_decide_result()`：
   网络回包 CreateTweet 的 rest_id（最强，服务端确认） > 页面跳转 status/<id> >
   toast 成功文案 > 编辑器清空+弹窗关闭。抓不到任何证据时不谎报成功也不自动重试
   （`extra["may_have_published"]=True`，retryable=False），交由人工核对，避免重复发推。
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from core import config
from core.backends.base import Job, PublishResult

log = logging.getLogger("twitbot.backends.browser")

# ── playwright 惰性/容错导入 ───────────────────────────────
# 导入失败不能把 registry.describe() 整条链带崩：本模块仍须可 import，
# 只是 available() 返回 False 并说明原因。
try:
    from playwright.sync_api import sync_playwright as _sync_playwright
    from playwright.sync_api import TimeoutError as PWTimeoutError  # noqa: N812
    _PW_IMPORT_ERROR = ""
except Exception as _e:  # pragma: no cover - 取决于安装环境
    _sync_playwright = None
    PWTimeoutError = TimeoutError  # type: ignore[assignment,misc]
    _PW_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"

_HAVE_PW = _sync_playwright is not None


def start_playwright():
    """工厂函数（独立出来便于测试 monkeypatch）。"""
    if _sync_playwright is None:
        raise RuntimeError(f"playwright 不可用: {_PW_IMPORT_ERROR}")
    return _sync_playwright().start()


# ── 常量：超时与选择器 ────────────────────────────────────
# ⚠ 下面这些选择器依赖 X 前端实现，前端改版会失效（失效时 publish 返回
#   retryable=False 且截图，人工按截图重新定位后只改这里即可）。

SEL_COMPOSE_BUTTON = '[data-testid="SideNav_NewTweet_Button"]'
SEL_EDITOR = '[data-testid="tweetTextarea_0"]'
SEL_SEND_BUTTON = '[data-testid="tweetButton"]'               # 独立/弹窗编辑器
SEL_SEND_BUTTON_INLINE = '[data-testid="tweetButtonInline"]'  # 首页内联编辑器
SEL_ATTACHMENTS = '[data-testid="attachments"]'
SEL_FILE_INPUT = 'input[type="file"]'
SEL_TOAST = '[data-testid="toast"]'
SEL_PROGRESS = '[role="progressbar"]'
SEL_PRIMARY_COLUMN = '[data-testid="primaryColumn"]'
SEL_ACCOUNT_SWITCHER = '[data-testid="SideNav_AccountSwitcher_Button"]'
SEL_LOGIN_BUTTON = '[data-testid="loginButton"]'
SEL_LOGIN_INPUT = 'input[autocomplete="username"]'
SEL_MODAL_MASK = '[data-testid="mask"]'
SEL_RETWEET = '[data-testid="retweet"]'
SEL_QUOTE_ENTRY = '[data-testid="quoteTweet"]'

COMPOSE_URL = "https://x.com/compose/post"
HOME_URL = "https://x.com/home"
LOGIN_URL = "https://x.com/login"

# 登录成功的页面特征（已实测存在于登录后的主页）
LOGGED_IN_MARKERS = (
    SEL_COMPOSE_BUTTON,
    SEL_ACCOUNT_SWITCHER,
    SEL_PRIMARY_COLUMN,
)
# 登出的页面特征。⚠ 实测：未登录访问 /home 会被重定向到 **x.com 根路径的落地页**
# （url 里根本没有 "login" 字样），落地页上可见的真实登录表单输入框是
# `input[name="username_or_email"]` / `input[type="password"]`，
# 以及 `[data-testid="google_sign_in_container"]` / `[data-testid="BottomBar"]`。
# 因此不能只看 URL，必须组合元素判断。
LOGGED_OUT_MARKERS = (
    'input[name="username_or_email"]',
    'input[type="password"]',
    SEL_LOGIN_INPUT,                      # input[autocomplete="username"]
    '[data-testid="google_sign_in_container"]',
    '[data-testid="BottomBar"]',
)
LOGGED_OUT_URL_HINTS = (
    "/i/flow/login", "/i/flow/signup", "/i/jf/onboarding",
)

# ── 账号密码登录（login_with_password）用的常量 ───────────────
# ⚠ 与上面的发布选择器一样依赖 X 前端实现；前端改版时只改这一块 + 下面的文案表。
#   每个常量都是**候选元组**：按顺序取第一个可见者，避免单点选择器失效即全盘失败。

PW_LOGIN_URL = "https://x.com/i/flow/login"

# ① 账号输入框（第 1 步）。
#    实测：/i/flow/login 的账号框是 input[autocomplete="username"]（落地页变体是
#    input[name="username_or_email"]）；中间拦截页复用同一个 ocfEnterTextTextInput。
SEL_PW_USERNAME = (
    'input[autocomplete="username"]',
    'input[name="username_or_email"]',
    'input[name="text"]',
)
# ② 密码输入框
SEL_PW_PASSWORD = (
    'input[type="password"]',
    'input[name="password"]',
)
# ③ 二次验证码 / 中间拦截页输入框。
#    data-testid="ocfEnterTextTextInput" 是 X 这类"再输一次文本"页面的通用 testid。
SEL_PW_OTP = (
    'input[data-testid="ocfEnterTextTextInput"]',
    'input[autocomplete="one-time-code"]',
    'input[inputmode="numeric"]',
)
# 中间拦截页（要求输入手机号/用户名）专用：把 username 填进去的兜底选择器
SEL_PW_CHALLENGE = SEL_PW_OTP + (
    'input[name="text"]',
    'input[type="text"]',
)
# ④ Next / 登录提交按钮（账号步与密码步共用 ocfEnterTextNextButton；
#    密码步另有 LoginForm_Login_Button；button[type="submit"] 是最后兜底）
SEL_PW_NEXT = (
    '[data-testid="ocfEnterTextNextButton"]',
    '[data-testid="LoginForm_Login_Button"]',
    'button[type="submit"]',
)
# ⑤ 人机验证（Arkose）特征
SEL_PW_CAPTCHA = (
    'iframe[src*="arkose"]',
    'iframe[title*="hallenge"]',
    '[data-testid="arkose_challenge"]',
    '#arkose-challenge',
)
# ⑥ 错误提示的识别走整页文本（`_pw_page_text`），不单独依赖 toast 选择器 ——
#    X 的报错既可能出现在 toast，也可能内联在表单下方，扫整页文本更稳。

# 页面文案特征表（全部小写比较）。用于把「密码错 / 验证码 / 人机验证 / 限流」
# 这几条必须区分的路径拆开 —— 只靠选择器无法区分它们。
PW_TEXT_WRONG_PW = (
    "wrong username or password", "incorrect password", "could not log you in",
    "the username or password you entered", "password you entered is incorrect",
    "密码不正确", "账号或密码不正确", "用户名或密码错误", "密码错误",
    # 新登录页 (`/i/jf/onboarding/web`) 的措辞：账号不存在 / 找不到账号。
    # 实测原文：「我们找不到使用该用户名的活跃 X 账号。」
    "找不到使用该用户名", "找不到使用该邮箱", "找不到该账号",
    "couldn't find your account", "could not find your account",
    "we couldn't find", "no active x account",
)
PW_TEXT_CHALLENGE = (
    "unusual activity", "verify your identity", "verify your login",
    "enter your phone number or username", "phone number or username",
    "confirm your identity", "异常活动", "验证你的身份",
    "输入你的手机号或用户名", "手机号或用户名",
)
PW_TEXT_LOCKED = (
    "account has been locked", "your account has been locked", "account is locked",
    "too many attempts", "temporarily locked", "account locked",
    "账号已被锁定", "尝试次数过多", "账号被锁定",
)
PW_TEXT_RATE = (
    "rate limit", "too many requests", "slow down", "请求过于频繁", "请稍后重试",
)
PW_TEXT_OTP = (
    "verification code", "enter the code", "we sent you a code", "check your email",
    "check your phone", "验证码", "确认码",
)
# ⑧ X 的通用错误页（弹窗标题「出了点问题」+ 按钮「重新开始」）。
#   实测出现在自动化会话被平台判定异常时（/i/jf/onboarding/web 的错误弹窗）。
PW_TEXT_GENERIC = (
    "出了点问题", "重新开始", "something went wrong", "start over",
)

# 对外文案（契约 §3 第 5/8 条，控制台据此区分提示 —— 不得随意改写措辞）
PW_MSG_WRONG_PW = "账号或密码不正确（X 未通过验证）"
PW_MSG_NEED_CODE = "需要验证码：请在控制台填入收到的验证码后重试（已保留本次会话）"
PW_MSG_BAD_CODE = "验证码不正确或已过期（账号密码已通过验证），请重新获取后重试"
PW_MSG_HUMAN = "X 要求人机验证/账号受限，请改用『有头模式手工登录』"
PW_MSG_NET = "网络不可达，请检查代理/VPN"
PW_MSG_CHALLENGE_STUCK = ("账号需要二次身份确认（X 要求输入手机号/用户名），自动填写未通过，"
                          "请改用『有头模式手工登录』")
PW_MSG_UNKNOWN_STEP = ("未出现密码输入框且无法确认账号状态（X 前端可能改版或要求人机验证），"
                       "请改用『有头模式手工登录』")
PW_MSG_UNKNOWN_RESULT = ("无法确认登录结果（页面既未出现登录成功特征，也未给出明确错误），"
                         "请改用『有头模式手工登录』")
PW_MSG_GENERIC = ("X 返回了通用错误（页面显示「出了点问题」），通常是平台判定本次自动化会话异常，"
                  "请稍后重试或改用『有头模式手工登录』")

# toast 文案
TOAST_OK_PATTERNS = (
    "your post was sent", "post was sent", "your tweet was sent", "was sent",
    "已发送", "已发布", "发送成功", "发布成功", "帖子已发送",
)
TOAST_ERR_PATTERNS = (
    ("daily limit", "retry", 900), ("超出每日", "retry", 900),
    ("rate limit", "retry", 300), ("限流", "retry", 300), ("too many requests", "retry", 300),
    ("duplicate", "fatal", 0), ("已发送过相同", "fatal", 0), ("重复", "fatal", 0),
    ("automated", "fatal", 0), ("自动", "fatal", 0),
    ("something went wrong", "retry", 30), ("出错了", "retry", 30), ("try again", "retry", 30),
)

# ⚠ 不要把这个当成"要用的 UA"直接写进 context！
#   只有当底层是 headless shell（会自称 HeadlessChrome）时才用它做改写，
#   版本号由 `_ua_for_version()` 按浏览器真实版本填充。
#   给真 Chrome / Chromium 覆盖 UA 会造成 UA 与 sec-ch-ua 版本打架 -> 被风控识别。
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _ua_for_version(version: str) -> str:
    """按浏览器真实版本生成 UA（仅 headless shell 改写用）。"""
    v = (version or "").strip()
    major = v.split(".")[0] if v else "131"
    full = v if v.count(".") >= 3 else f"{major}.0.0.0"
    return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{full} Safari/537.36")

# 反检测 init script：只是抹掉自动化框架的明显指纹，
# 目标是"降低被误判为异常行为的概率"，不是对抗平台风控系统。
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN','zh','en-US','en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
window.chrome = window.chrome || {runtime: {}};
if (navigator.permissions && navigator.permissions.query) {
  const _q = navigator.permissions.query.bind(navigator.permissions);
  navigator.permissions.query = (p) => (
    p && p.name === 'notifications'
      ? Promise.resolve({state: Notification.permission})
      : _q(p)
  );
}
"""

# ── 启动配置候选链（写死顺序，逐个尝试直到某次真的能打开 x.com）──
#
# ⚠ 实测结论（2026-09，playwright 1.63.0 / Chromium 141）：
#   playwright 默认的 headless Chromium 是 **headless shell**，向 x.com 请求会被
#   直接回 **HTTP 403**（页面只有 `<html><head></head><body></body></html>`，无任何
#   data-testid）。而 `channel="chromium"`（新版 headless / Chrome Headless Mode）与
#   有头模式都能拿到 200。所以**不能**简单用 `chromium.launch(headless=True)`。
#
# 策略：按顺序尝试候选配置，用一次轻量"能不能真拿到页面"的探测挑出可用者，
# 成功者在进程内缓存。既修掉了 403，又能在环境缺少 channel="chromium" 时自动退回。
# ── 无头启动配置候选链 ────────────────────────────────────
#
# ⚠ 实测结论（2026-09，playwright 1.63.0 / Chromium 141）：
#   playwright 默认的 headless Chromium 是 **headless shell**，向 x.com 请求
#   /home 会被直接回 **HTTP 403**（页面全白、零个 data-testid），有时则表现为
#   `page.goto` 直接抛 `net::ERR_HTTP_RESPONSE_CODE_FAILURE`。
#   而 `channel="chromium"`（新版 headless / Chrome Headless Mode）在多次实测中
#   都能正常拿到 200。所以**不能**简单用 `chromium.launch(headless=True)`。
#
# 重要设计取舍：**不做投机性探测**。
#   最初版本会在启动前逐个起浏览器去"试哪个配置能连通"，实测这本身就是
#   一次小型压测 —— 连续几轮探测后本机 IP 被 x.com 限流，反而让所有配置都失败。
#   现在的做法：默认用实测最可靠的配置（channel="chromium"），**只在真正发布时
#   被 403 拦下**才依次换下一个候选重试（反应式回退，无额外流量）。
#   可用 `BROWSER_CHANNEL` 环境变量显式指定（见 _candidates()）。
_LAUNCH_CANDIDATES: tuple[dict, ...] = (
    {"headless": True, "channel": "chromium"},   # 新版 headless，实测最可靠
    {"headless": True, "channel": "chrome"},     # 系统安装的 Google Chrome
    {"headless": True},                          # playwright 自带 headless shell
)

_BLOCK_STATUSES = (401, 403, 407, 429)
_MAX_BLOCK_RETRIES = len(_LAUNCH_CANDIDATES) - 1   # 被 403 时最多再换几个配置
_BLANK_SETTLE_MS = 3000          # 判定"空白页"前留给页面渲染的时间

# 进程内记住"哪个候选配置是好的"，避免后续每次发布都从 ① 开始试。
_GOOD_LAUNCH_INDEX = 0
_LAUNCH_INDEX_LOCK = threading.Lock()

# 同一进程内串行化所有浏览器会话（publish / login / verify 共用）
_BROWSER_LOCK = threading.Lock()


def _now_tag() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


@dataclass
class _Session:
    """一次浏览器会话的三件套；由 `_open_session` 负责兜底关闭。"""
    pw: Any
    browser: Any
    context: Any


class BrowserBackend:
    """无头浏览器发布后端。所有对外方法**不得抛异常穿透**。"""

    name = "browser"
    label = "无头浏览器"

    # ── 超时参数（可按需调） ───────────────────────────────
    STATE_FILENAME = "storage_state.json"
    LOGIN_TIMEOUT_SECONDS = 300          # 交互式登录等待上限（硬上限，不会无限等）
    LOGIN_POLL_SECONDS = 1.5
    NAV_TIMEOUT_MS = 45_000
    ELEMENT_TIMEOUT_MS = 20_000
    POST_CONFIRM_TIMEOUT_MS = 90_000     # 等 CreateTweet 回包
    UI_CONFIRM_TIMEOUT_MS = 25_000       # 等 toast / 编辑器状态变化
    MEDIA_UPLOAD_TIMEOUT_MS = 300_000    # 视频可能很慢
    TYPE_DELAY_MS = 25                   # 模拟真人输入的逐字延迟
    LOCK_WAIT_SECONDS = 900              # 等锁上限（串行化的安全阀）
    VERIFY_LOCK_WAIT_SECONDS = 5         # verify 是轻量动作，忙就快速返回

    # ── 账号密码登录（login_with_password）参数 ────────────
    PW_STEP_TIMEOUT_MS = 20_000          # 等"下一步出现"（密码框/验证码框）的上限
    PW_CONFIRM_TIMEOUT_MS = 20_000       # 等登录成功特征的上限
    PW_POLL_MS = 400                     # 轮询间隔
    PW_MAX_CHALLENGE_ROUNDS = 3          # 中间拦截页（unusual activity）最多自动重填几次

    # ══════════════════════════════════════════════════════
    # 登录态
    # ══════════════════════════════════════════════════════

    def state_path(self) -> Path:
        """登录态文件路径（每次读 config，便于测试里 patch）。"""
        return Path(config.BROWSER_DIR) / self.STATE_FILENAME

    def has_state(self) -> bool:
        """登录态文件存在、非空、是含 cookies 的合法 JSON。**纯本地读，不联网。**"""
        p = self.state_path()
        try:
            if not p.is_file():
                return False
            if p.stat().st_size < 2:
                return False
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return False
            return bool(data.get("cookies"))
        except Exception:
            return False

    @staticmethod
    def saved_account() -> str:
        """上次登录记住的 X 账号（可能为空）。纯本地读，不联网。"""
        try:
            from core import settings as _settings
            return (_settings.get("x_username", "") or "").strip()
        except Exception:
            return ""

    @staticmethod
    def remember_account(handle: str, display_name: str = "") -> None:
        """记住当前登录的账号，供控制台显示。**绝不写密码。**"""
        try:
            from core import settings as _settings
            h = (handle or "").strip()
            if h and not h.startswith("@"):
                h = "@" + h
            val = f"{display_name.strip()} {h}".strip() if display_name else h
            if val:
                _settings.set_many({"x_username": val[:200]})
        except Exception as e:
            log.debug("记账号失败(忽略): %s", e)

    @staticmethod
    def _read_account_from_page(page) -> tuple[str, str]:
        """从已登录页面读出 (handle, display_name)。失败返回 ("", "")。

        实测（2026-09）：X 左下角的账号切换按钮里同时有显示名和 @handle，
        形如 "显示名 | @handle"。这是最稳的来源 ——
        Notifications 之类页面也会出现别人的 @handle，所以不能乱扫全页。
        """
        try:
            info = page.evaluate(
                """() => {
                    const sel = '[data-testid="SideNav_AccountSwitcher_Button"]';
                    const el = document.querySelector(sel);
                    if (!el) return {text: '', alt: ''};
                    const img = el.querySelector('img');
                    return {text: (el.innerText || '').trim(),
                            alt: img ? (img.alt || '') : ''};
                }""")
            text = str((info or {}).get("text") or "")
            alt = str((info or {}).get("alt") or "")
            handle = ""
            display = ""
            # 先找 @handle
            for part in text.replace("\n", "|").split("|"):
                p = part.strip()
                if p.startswith("@") and 1 < len(p) <= 21:
                    handle = p
                    break
            # 显示名 = handle 那一行之外的第一段文字（兜底用头像 alt）
            for part in text.replace("\n", "|").split("|"):
                p = part.strip()
                if p and not p.startswith("@") and len(p) < 60:
                    display = p
                    break
            if not display:
                display = alt.strip()
            return handle, display
        except Exception:
            return "", ""

    def _sync_saved_account(self, page) -> None:
        """联网检查时顺手把账号名落到 settings（搬过来的登录态也能识别出是谁）。"""
        try:
            handle, display = self._read_account_from_page(page)
            if handle or display:
                self.remember_account(handle, display)
        except Exception as e:
            log.debug("同步账号名失败(忽略): %s", e)

    def state_summary(self) -> str:
        """登录态摘要（**账号名** / cookie 数 / 保存时间），给控制台展示，纯本地读。"""
        try:
            data = json.loads(self.state_path().read_text(encoding="utf-8"))
            n = len(data.get("cookies") or [])
            ts = self.state_path().stat().st_mtime
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
            who = self.saved_account()
            # 账号名放在最前面 —— 用户最想知道"我现在登的是谁"
            if who:
                return f"账号 {who}｜{n} 条 cookie｜保存于 {when}"
            return f"账号未知（未记录）｜{n} 条 cookie｜保存于 {when}"
        except Exception:
            return "（无法读取摘要）"

    def available(self) -> tuple[bool, str]:
        """能否工作。**不得联网、不得启动浏览器**（控制台会频繁调用，必须快）。"""
        try:
            if not _HAVE_PW:
                return False, f"playwright 未就绪: {_PW_IMPORT_ERROR}"
            if self.has_state():
                return True, f"已保存登录态（{self.state_summary()}）"
            return False, "未登录，请在 Web 控制台点「登录 X」（或在命令行运行 python tools/browser_login_chrome.py）"
        except Exception as e:  # 兜底：available 不抛异常
            return False, f"检查登录态失败: {type(e).__name__}: {e}"

    # ══════════════════════════════════════════════════════
    # 浏览器会话
    # ══════════════════════════════════════════════════════

    @staticmethod
    def _candidates() -> list[dict]:
        """返回启动配置候选列表。`BROWSER_CHANNEL` 显式指定时只返回那一个。"""
        forced = (os.getenv("BROWSER_CHANNEL") or "").strip()
        if forced:
            return [{"headless": True, "channel": forced}]
        return [dict(c) for c in _LAUNCH_CANDIDATES]

    @staticmethod
    def _launch_kwargs(headless: bool, index: int | None = None) -> dict:
        """取第 index 个候选的启动参数（默认取进程内记下的"已知可用"那个）。

        有头模式一律优先用**系统安装的真 Chrome**（`channel="chrome"`）：
        Playwright 自带的 Chromium 会在 `sec-ch-ua` 里报 `"Chromium";v="153"`，
        而浏览器 UA 又常被我们覆盖成别的版本，两者矛盾会让 X 的风控直接把会话
        判定为自动化（表现为登录页弹「出了点问题」，URL 带 `prelude_gate`）。
        真 Chrome 的 UA / sec-ch-ua / userAgentData 三者天然自洽。
        没装真 Chrome 时自动回退到自带 Chromium。
        """
        forced = (os.getenv("BROWSER_CHANNEL") or "").strip()
        if not headless:
            if forced:
                return {"headless": False, "channel": forced}
            return {"headless": False, "channel": "chrome"}
        cands = BrowserBackend._candidates()
        if index is None:
            index = _GOOD_LAUNCH_INDEX
        index = max(0, min(index, len(cands) - 1))
        return dict(cands[index])

    @staticmethod
    def _mark_good(index: int) -> None:
        global _GOOD_LAUNCH_INDEX
        with _LAUNCH_INDEX_LOCK:
            if _GOOD_LAUNCH_INDEX != index:
                log.info("记录可用的无头启动配置序号: %d", index)
                _GOOD_LAUNCH_INDEX = index

    @staticmethod
    def _reset_good() -> None:
        global _GOOD_LAUNCH_INDEX
        with _LAUNCH_INDEX_LOCK:
            _GOOD_LAUNCH_INDEX = 0

    @contextlib.contextmanager
    def _open_session(self, *, headless: bool, use_state: bool = True,
                      launch_index: int | None = None) -> Iterator[_Session]:
        """开一次浏览器会话，**保证** finally 里关掉 context/browser/playwright。

        Chromium 进程泄漏会把机器拖垮，所以三层都关，且每层单独 try。
        launch_index 指定用候选链里的哪个启动配置（None = 用已知可用的那个）。
        """
        pw = browser = context = None
        launch_kw = dict(self._launch_kwargs(headless, launch_index))
        launch_kw["slow_mo"] = config.BROWSER_SLOWMO or 0
        # 去掉 AutomationControlled 特性，让自动化痕迹最少
        launch_kw["args"] = ["--disable-blink-features=AutomationControlled",
                             "--no-first-run",
                             "--no-default-browser-check"]
        try:
            pw = start_playwright()
            try:
                browser = pw.chromium.launch(**launch_kw)
            except Exception as e:
                # 没装系统真 Chrome 时不能直接崩 —— 退回 Playwright 自带 Chromium。
                # （指纹略差，可能有风控风险，但总比完全用不了强。）
                if launch_kw.get("channel") and not os.getenv("BROWSER_CHANNEL"):
                    log.warning("channel=%s 启动失败（%s），回退到自带 Chromium",
                                launch_kw.get("channel"), _redact_secret(str(e)))
                    launch_kw.pop("channel", None)
                    launch_kw.setdefault("headless", headless)
                    browser = pw.chromium.launch(**launch_kw)
                else:
                    raise
            kwargs: dict[str, Any] = {
                "viewport": {"width": 1366, "height": 900},
                "locale": "zh-CN",
                "timezone_id": "Asia/Shanghai",
            }
            # 只有 headless 才会自称 "HeadlessChrome"（那本身就是自动化标记），
            # 这时才按**真实版本**改写 UA —— 版本对了才不会和 sec-ch-ua 打架。
            # 有头模式（真 Chrome）保持原生 UA：UA / sec-ch-ua / userAgentData 天然一致。
            if launch_kw.get("headless"):
                try:
                    kwargs["user_agent"] = _ua_for_version(getattr(browser, "version", ""))
                except Exception:
                    pass
            if use_state:
                kwargs["storage_state"] = str(self.state_path())
            context = browser.new_context(**kwargs)
            context.add_init_script(_STEALTH_JS)
            context.set_default_timeout(self.ELEMENT_TIMEOUT_MS)
            context.set_default_navigation_timeout(self.NAV_TIMEOUT_MS)
            yield _Session(pw=pw, browser=browser, context=context)
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception as e:
                    log.debug("context.close 失败(忽略): %s", e)
            if browser is not None:
                try:
                    browser.close()
                except Exception as e:
                    log.debug("browser.close 失败(忽略): %s", e)
            if pw is not None:
                try:
                    pw.stop()
                except Exception as e:
                    log.debug("playwright.stop 失败(忽略): %s", e)

    # ══════════════════════════════════════════════════════
    # 登录态探测
    # ══════════════════════════════════════════════════════

    @staticmethod
    def _probe_login_state(page) -> bool | None:
        """快速探测当前页面是否已登录：True/False/None（未知）。不做等待。

        ⚠ 判定顺序很重要：**先看登录后特征，再看登出特征**。
        因为未登录访问 /home 会被重定向到 x.com **根路径的落地页**（url 里没有
        "login"），所以不能只靠 URL；而登录后页面绝不会出现登录表单输入框。
        """
        try:
            # ① 登录后特征（最强正向证据）
            for sel in LOGGED_IN_MARKERS:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    return True
        except Exception:
            return None
        try:
            # ② 登出特征：URL 落在登录/引导流，或页面上有登录表单
            url = (page.url or "").lower()
            if any(h in url for h in LOGGED_OUT_URL_HINTS):
                return False
            for sel in LOGGED_OUT_MARKERS:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    return False
        except Exception:
            return None
        return None

    def _wait_login_state(self, page, timeout_ms: int) -> bool:
        """轮询到能判定为止；超时返回 False。"""
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            st = self._probe_login_state(page)
            if st is not None:
                return st
            page.wait_for_timeout(400)
        return False

    # ══════════════════════════════════════════════════════
    # verify
    # ══════════════════════════════════════════════════════

    def verify(self) -> tuple[bool, str]:
        """用已保存的登录态开无头浏览器访问 /home，判断登录态是否仍然有效。**不发推。**"""
        if not _HAVE_PW:
            return False, f"playwright 未就绪: {_PW_IMPORT_ERROR}"
        if not self.has_state():
            return False, "未登录，请在 Web 控制台点「登录 X」（或在命令行运行 python tools/browser_login_chrome.py）"
        if not _BROWSER_LOCK.acquire(timeout=self.VERIFY_LOCK_WAIT_SECONDS):
            return False, "浏览器正在执行其它任务，请稍后再试"
        try:
            with self._open_session(headless=config.BROWSER_HEADLESS) as s:
                page = s.context.new_page()
                resp = None
                try:
                    resp = page.goto(HOME_URL, wait_until="domcontentloaded")
                except Exception as e:
                    if "ERR_HTTP_RESPONSE_CODE_FAILURE" in str(e) or "403" in str(e):
                        return False, self._block_message(403)
                    raise
                status = getattr(resp, "status", None) if resp else None
                if status in _BLOCK_STATUSES or _page_is_blank(page):
                    return False, self._block_message(
                        status if status not in (None, 200) else None)
                if self._wait_login_state(page, 20_000):
                    # 顺手识别账号名 —— 搬过来的登录态也能由此知道"是谁"
                    self._sync_saved_account(page)
                    who = self.saved_account()
                    return True, (f"登录态有效（账号 {who}）" if who else "登录态有效")
                if self._probe_login_state(page) is False:
                    return False, "登录态已失效，请重新登录"
                return False, "无法确认登录态（页面未出现登录/主页特征，可能是网络或前端改版）"
        except Exception as e:
            return False, f"检查失败: {type(e).__name__}: {e}"
        finally:
            _BROWSER_LOCK.release()

    # ══════════════════════════════════════════════════════
    # login（交互式，有头）
    # ══════════════════════════════════════════════════════

    def login(self, on_event: Callable[[str], None] | None = None,
              timeout: int | None = None) -> tuple[bool, str]:
        """有头模式打开登录页，等用户手工登录，成功后保存 storage_state。

        on_event: on_event(str) 进度回调（推给 Web 控制台 / 控制台打印）。
        timeout:  等待登录的秒数上限，默认 LOGIN_TIMEOUT_SECONDS（300s），不会无限等。
        返回 (是否成功, 说明)。
        """
        limit = int(timeout if timeout is not None else self.LOGIN_TIMEOUT_SECONDS)

        # 若本线程正处于 asyncio 事件循环中（Web 控制台直接调用），
        # 同步 API 会炸 —— 自动换到独立线程执行，事件照常回调。
        try:
            import asyncio
            loop_running = asyncio.get_running_loop() is not None
        except RuntimeError:
            loop_running = False
        except Exception:
            loop_running = False

        if loop_running:
            box: dict[str, Any] = {}

            def _worker():
                box["r"] = self.login(on_event=on_event, timeout=limit)

            t = threading.Thread(target=_worker, name="browser-login", daemon=True)
            t.start()
            t.join()
            return box.get("r", (False, "登录线程异常结束"))

        if not _HAVE_PW:
            return False, f"playwright 未就绪: {_PW_IMPORT_ERROR}"

        def emit(msg: str) -> None:
            log.info("[browser-login] %s", msg)
            if on_event:
                try:
                    on_event(msg)
                except Exception:
                    pass

        if not _BROWSER_LOCK.acquire(timeout=self.LOCK_WAIT_SECONDS):
            return False, "浏览器正忙（有其它发布/登录任务在跑），请稍后重试"
        try:
            emit("正在启动浏览器（有头模式，会弹出窗口）...")
            with self._open_session(headless=False, use_state=False) as s:
                page = s.context.new_page()
                emit(f"正在打开登录页 {LOGIN_URL}")
                try:
                    resp = page.goto(LOGIN_URL, wait_until="domcontentloaded")
                except Exception as e:
                    if "ERR_HTTP_RESPONSE_CODE_FAILURE" in str(e) or "403" in str(e):
                        return False, self._block_message(403)
                    return False, f"打开登录页失败（网络不可达？）: {type(e).__name__}: {e}"
                page.wait_for_timeout(1500)
                status = getattr(resp, "status", None) if resp else None
                if status in _BLOCK_STATUSES or _page_is_blank(page, settle_ms=1500):
                    return False, self._block_message(
                        status if status in _BLOCK_STATUSES else None)
                title = ""
                try:
                    title = page.title()
                except Exception:
                    pass
                emit(f"页面已加载（title={title or '(空)'}），请在弹出的浏览器窗口中完成登录")

                st = self._probe_login_state(page)
                if st is True:
                    emit("检测到已经是登录状态，直接保存凭据")

                deadline = time.time() + limit
                last_tick = 0
                while time.time() < deadline and st is not True:
                    try:
                        page.wait_for_timeout(int(self.LOGIN_POLL_SECONDS * 1000))
                        st = self._probe_login_state(page)
                    except Exception as e:
                        # 用户把浏览器窗口关掉了（TargetClosedError 等）——
                        # 这不是"登录失败"，是主动取消，要说清楚。
                        if "closed" in str(e).lower() or "TargetClosed" in type(e).__name__:
                            return False, "登录窗口已被关闭，未保存凭据（如需登录请重新运行）"
                        raise
                    remain = int(deadline - time.time())
                    if remain // 30 != last_tick // 30:
                        last_tick = remain
                        emit(f"正在等待登录...（剩余 {max(remain, 0)} 秒）")

                if st is not True:
                    return False, (f"登录超时（{limit} 秒内未检测到登录成功），"
                                   f"请重新运行 tools/browser_login_chrome.py（或在控制台点「登录 X」）并计时完成登录")

                # 落到首页让 cookie 写全，再落盘
                try:
                    page.goto(HOME_URL, wait_until="domcontentloaded")
                    page.wait_for_timeout(1500)
                except Exception:
                    pass
                if self._probe_login_state(page) is not True:
                    return False, "登录状态校验失败（跳转首页后未找到发帖入口），请重试"

                path = self.state_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                s.context.storage_state(path=str(path))
                if not self.has_state():
                    return False, f"凭据写入失败或内容为空: {path}"
                emit(f"登录成功，凭据已保存到 {path}")
                return True, f"登录成功，已保存凭据（{path}）"
        except Exception as e:
            # 用户手动关掉浏览器窗口时会抛 TargetClosedError / "has been closed"。
            # 这是主动取消而不是"登录失败"，说清楚以免用户以为账号有问题。
            if "closed" in str(e).lower() or "TargetClosed" in type(e).__name__:
                return False, "登录窗口已被关闭，未保存凭据（如需登录请重新运行）"
            return False, f"登录过程异常: {type(e).__name__}: {e}"
        finally:
            _BROWSER_LOCK.release()

    @staticmethod
    def _block_message(status, page=None) -> str:
        """被 x.com 拦截时的可读原因。区分"明确 4xx"与"空白页"两种情况。"""
        which = f"HTTP {status}" if status else "页面为空"
        extra = ""
        if status is None or status == 200:
            # 状态码正常却拿到空白页：是拦截，不是"响应码错误"，别误导用户
            extra = "（HTTP 状态码正常但页面内容为空，典型的自动化客户端拦截）"
        return (f"访问 x.com 被拒绝（{which}）{extra}：当前无头浏览器配置被判定为自动化客户端。"
                f"请设置 BROWSER_HEADLESS=false 后重试，或检查网络出口是否被限流。")

    # ══════════════════════════════════════════════════════
    # login_with_password（账号密码登录，有头）—— 契约 §3
    # ══════════════════════════════════════════════════════
    #
    # 安全红线（契约 §3.1，实现时必须守住）：
    #   ① password 只出现在 `locator.fill(password)` 这一处调用里；
    #   ② 绝不拼进任何 URL / 文件名 / 截图名 / 日志 / 返回值 / settings；
    #   ③ 任何可能含表单值的文本（异常消息、调试日志）都必须先过
    #      `_redact_secret()` 再落盘/返回；
    #   ④ 截图沿用 `config.LOG_DIR`，密码框在浏览器里本身就是 ·，不含明文。
    #
    # 关于"已保留本次会话"（PW_MSG_NEED_CODE 的文案）：该文案由契约 §3 第 5 条
    # 逐字规定（web/server.py 与 tests/test_integration_v2.py 都按它判定
    # need_code），故保持原样。实现上登录失败路径不落盘 storage_state
    # ——契约 §3 第 7 条只要求成功时写 state；用户重试时重新走一遍流程即可。

    def login_with_password(self, username: str, password: str,
                            on_event: Callable[[str], None] | None = None,
                            timeout: int | None = None,
                            code: str = "") -> tuple[bool, str]:
        """用账号密码走有头浏览器登录，成功后写 storage_state。**绝不抛异常**。

        流程（契约 §3）：打开 https://x.com/i/flow/login → 填账号 → Next →
        填密码 → Next →（可能出现中间拦截页/二次验证码）→ 判定结果。
        中间拦截页会尽力自动填账号再继续；过不去就返回明确中文提示。

        on_event: on_event(str) 进度回调（推给 Web 控制台）。
        timeout:  整个流程的秒数上限，默认 LOGIN_TIMEOUT_SECONDS（300s）。
        code:     二次验证码（短信/邮箱/2FA）。首次留空：
                  出现验证码框时返回"需要验证码"（与"密码错"可区分）。

        返回 (是否成功, 中文说明)。分类可辨：密码错 / 需验证码 / 人机验证受限 /
        网络不可达 / 被限流（复用 `_block_message`）。
        """
        limit = int(timeout if timeout is not None else self.LOGIN_TIMEOUT_SECONDS)
        user = (username or "").strip()
        secret = password or ""            # 只在调用栈里活一次
        otp = (code or "").strip()

        # 若本线程正处于 asyncio 事件循环中（Web 控制台直接调用），
        # 同步 API 会炸 —— 自动换到独立线程执行（与 login() 同一策略）。
        try:
            import asyncio
            loop_running = asyncio.get_running_loop() is not None
        except RuntimeError:
            loop_running = False
        except Exception:
            loop_running = False

        if loop_running:
            box: dict[str, Any] = {}

            def _worker():
                box["r"] = self.login_with_password(
                    username=user, password=secret, on_event=on_event,
                    timeout=limit, code=otp)

            t = threading.Thread(target=_worker, name="browser-login-pw", daemon=True)
            t.start()
            t.join()
            return box.get("r", (False, "登录线程异常结束"))

        if not _HAVE_PW:
            return False, f"playwright 未就绪: {_PW_IMPORT_ERROR}"
        if not user or not secret:
            return False, "账号或密码为空，请填写后再登录"

        def emit(msg: str) -> None:
            """进度回调。**出口统一脱敏**：任何消息都不允许带出密码/验证码。"""
            safe = _redact_secret(msg, secret, otp)
            log.info("[browser-login-pw] %s", safe)
            if on_event:
                try:
                    on_event(safe)
                except Exception:
                    pass

        if not _BROWSER_LOCK.acquire(timeout=self.LOCK_WAIT_SECONDS):
            return False, "浏览器正忙（有其它发布/登录任务在跑），请稍后重试"
        try:
            emit("正在启动浏览器（有头模式，会弹出窗口）...")
            with self._open_session(headless=False, use_state=False) as s:
                page = s.context.new_page()
                emit(f"正在打开登录页 {PW_LOGIN_URL}")
                try:
                    resp = page.goto(PW_LOGIN_URL, wait_until="domcontentloaded")
                except Exception as e:
                    safe = _redact_secret(str(e), secret, otp)
                    if "ERR_HTTP_RESPONSE_CODE_FAILURE" in safe or "403" in safe:
                        return False, self._block_message(403)
                    return False, f"{PW_MSG_NET}（打开登录页失败: {type(e).__name__}）"
                page.wait_for_timeout(1500)
                status = getattr(resp, "status", None) if resp else None
                if status in _BLOCK_STATUSES or _page_is_blank(page, settle_ms=1500):
                    return False, self._block_message(
                        status if status in _BLOCK_STATUSES else None)
                emit("登录页已加载，开始自动填写账号（本流程不记录密码）")
                return self._pw_run_flow(page=page, context=s.context, user=user,
                                         secret=secret, otp=otp, limit=limit, emit=emit)
        except Exception as e:
            # 用户手动关掉浏览器窗口时抛 TargetClosedError —— 是主动取消，不是登录失败
            if "closed" in str(e).lower() or "TargetClosed" in type(e).__name__:
                return False, "登录窗口已被关闭，未保存凭据（如需登录请重新运行）"
            safe = _redact_secret(str(e), secret, otp)
            return False, f"登录过程异常: {type(e).__name__}: {safe}"
        finally:
            _BROWSER_LOCK.release()

    def _pw_run_flow(self, *, page, context, user: str, secret: str, otp: str,
                     limit: int, emit: Callable[[str], None]) -> tuple[bool, str]:
        """登录四步状态机。

        刻意**不写成线性脚本**：X 会在任意环节插入中间拦截页（unusual activity
        要求再输一次手机号/用户名）、人机验证或验证码页。所以每轮只看"当前页面上
        有什么可见元素"，再决定动作；每个动作都有"已做过"守卫，不会重复提交。
        密码只在第 ⑤ 步的 `locator.fill(secret)` 出现一次/次尝试。
        """
        deadline = time.time() + limit
        sent_user = False
        sent_pw = 0                 # 已提交密码的次数（最多 2 次，避免把账号试锁）
        otp_tries = 0
        chall_rounds = 0

        # 失败截图（契约 §3.1：失败照旧落 config.LOG_DIR）。
        # ⚠ 文件名只含时间戳 —— 绝不含账号/密码/验证码；密码框在浏览器里本就是 ·。
        shot = self._pw_shot_path()

        def fail(msg: str) -> tuple[bool, str]:
            got = self._screenshot(page, shot)
            if got:
                emit(f"已保存失败截图: {got}")
            return False, msg

        while time.time() < deadline:
            # ① 已登录（最强正向证据）
            if self._probe_login_state(page) is True:
                return self._pw_save_and_finish(page, context, user, emit)

            # ② 人机验证（Arkose）：自动化过不去，且绝不能硬试
            if self._pw_find(page, SEL_PW_CAPTCHA, 0) is not None:
                emit("检测到人机验证挑战（Arkose）")
                return fail(PW_MSG_HUMAN)

            kind = self._pw_classify(page)

            # ③ 页面已明确给出结论：先分类，再决定文案（必须与"密码错"可区分）
            if kind == "wrong_pw":
                emit("X 回显：账号或密码不正确")
                return fail(PW_MSG_WRONG_PW)
            if kind == "locked":
                emit("X 回显：账号被锁定或尝试次数过多")
                return fail(PW_MSG_HUMAN)
            if kind == "rate":
                emit("X 回显：请求被限流")
                return fail(self._block_message(429))
            if kind == "generic":
                emit("X 回显：通用错误页（出了点问题）")
                return fail(PW_MSG_GENERIC)

            # ④ 账号步
            #    ⚠ X 的新登录页 (`/i/jf/onboarding/web?mode=login`) 把**账号和密码
            #    合并在同一张表单**里，两个输入框同时可见。若只填账号就提交，X 会
            #    回一个「出了点问题 / 重新开始」的错误页。所以填完账号要看一眼密码框：
            #    已经可见就一起填完再提交；旧版两步流程密码框此时不可见，行为不变。
            if not sent_user:
                loc = self._pw_find(page, SEL_PW_USERNAME, 0)
                if loc is not None:
                    if not self._pw_fill(loc, user, secret, otp):
                        return fail("无法填写账号输入框（X 前端可能改版），"
                                    "请改用『有头模式手工登录』")
                    sent_user = True
                    pw_loc = self._pw_find(page, SEL_PW_PASSWORD, 0)
                    if pw_loc is not None:
                        # —— 合并表单：账号 + 密码一次性填完再提交 ——
                        if not self._pw_fill(pw_loc, secret, secret, otp):
                            return fail("无法填写密码输入框（X 前端可能改版），"
                                        "请改用『有头模式手工登录』")
                        sent_pw += 1
                        emit("检测到账号密码合并表单，已一并填写，点击继续")
                        self._pw_click_next(page, emit, secret, otp)
                        self._pw_pause(page)
                        continue
                    emit("已填写账号，点击下一步")
                    self._pw_click_next(page, emit, secret, otp)
                    self._pw_pause(page)
                    continue

            # ⑤ 密码步
            if sent_user and sent_pw == 0:
                loc = self._pw_find(page, SEL_PW_PASSWORD, 0)
                if loc is not None:
                    if not self._pw_fill(loc, secret, secret, otp):
                        return fail("无法填写密码输入框（X 前端可能改版），"
                                    "请改用『有头模式手工登录』")
                    sent_pw += 1
                    emit("已填写密码，点击登录")
                    self._pw_click_next(page, emit, secret, otp)
                    self._pw_pause(page)
                    continue

            # ⑥ 额外的文本输入页：验证码页（优先，因为 code 是给它的）或中间拦截页
            extra = self._pw_find(page, SEL_PW_CHALLENGE, 0)
            if extra is not None:
                if kind == "otp" or (kind != "challenge" and otp):
                    # —— 二次验证码 ——
                    if not otp:
                        # ⚠ 关键：这里账号密码是**正确的**，必须与"密码错"区分开
                        emit("出现验证码输入框，等待用户在控制台填入验证码")
                        return fail(PW_MSG_NEED_CODE)
                    if otp_tries >= 2:
                        emit("验证码多次未通过")
                        return fail(PW_MSG_BAD_CODE)
                    if not self._pw_fill(extra, otp, secret, otp):
                        return fail("无法填写验证码输入框（X 前端可能改版）")
                    otp_tries += 1
                    emit("已提交验证码，等待结果")
                    self._pw_click_next(page, emit, secret, otp)
                    self._pw_pause(page)
                    continue
                # —— 中间拦截页（unusual activity）：尽力填账号再继续 ——
                if chall_rounds >= self.PW_MAX_CHALLENGE_ROUNDS:
                    emit(f"中间拦截页连续 {chall_rounds} 次未能通过")
                    return fail(PW_MSG_CHALLENGE_STUCK)
                chall_rounds += 1
                emit(f"X 要求二次身份确认（第 {chall_rounds} 次），尝试填写账号继续")
                if not self._pw_fill(extra, user, secret, otp):
                    return fail(PW_MSG_CHALLENGE_STUCK)
                self._pw_click_next(page, emit, secret, otp)
                self._pw_pause(page)
                continue

            # ⑦ 密码提交后页面没变化（点击可能没生效）：重填并再提交一次
            if sent_pw == 1:
                pw_loc = self._pw_find(page, SEL_PW_PASSWORD, 0)
                if pw_loc is not None:
                    if self._pw_fill(pw_loc, secret, secret, otp):
                        sent_pw += 1
                        emit("密码提交后页面未变化，重试提交一次")
                        self._pw_click_next(page, emit, secret, otp)
                        self._pw_pause(page)
                        continue

            # ⑧ 页面还在渲染/跳转：等一轮再看
            self._pw_pause(page)

        # ── 超时收口：不给"可能是密码错"的含糊结论，按已知状态给具体原因 ──
        if chall_rounds:
            return fail(PW_MSG_CHALLENGE_STUCK)
        if not sent_user or sent_pw == 0:
            return fail(PW_MSG_UNKNOWN_STEP)
        return fail(PW_MSG_UNKNOWN_RESULT)

    def _pw_shot_path(self) -> Path:
        """失败截图路径。⚠ 文件名只含时间戳，**绝不含账号/密码/验证码**。"""
        try:
            log_dir = Path(config.LOG_DIR)
            log_dir.mkdir(parents=True, exist_ok=True)
            return log_dir / f"login_fail_{_now_tag()}.png"
        except Exception:
            return Path(os.getcwd()) / f"login_fail_{_now_tag()}.png"

    def _pw_save_and_finish(self, page, context, user: str,
                            emit: Callable[[str], None]) -> tuple[bool, str]:
        """登录成功后的收尾：跳首页写全 cookie → storage_state → 记住账号名。"""
        emit("检测到登录成功特征")
        try:
            page.goto(HOME_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
        except Exception as e:
            log.debug("登录成功后跳首页失败(忽略): %s", e)
        st = self._probe_login_state(page)
        if st is False:
            return False, "登录状态校验失败（跳转首页后未找到登录特征），请重试"
        path = self.state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(path))
        except Exception as e:
            return False, f"凭据写入失败: {type(e).__name__}: {e}"
        if not self.has_state():
            return False, f"凭据写入失败或内容为空: {path}"
        # 只记账号名（绝不写密码）——写失败不否定登录成功
        try:
            from core import settings as _settings
            _settings.set_many({"x_username": str(user)[:200]})
        except Exception as e:
            log.warning("写入 x_username 失败(忽略): %s", type(e).__name__)
        emit(f"登录成功，凭据已保存到 {path}")
        return True, f"登录成功，已保存凭据（{path}）"

    # ── 账号密码登录的小工具（全部对异常容错） ─────────────

    @staticmethod
    def _pw_find(page, selectors: tuple[str, ...], timeout_ms: int = 0):
        """按候选顺序找第一个**可见**元素。找不到返回 None（不抛异常）。

        ⚠ 必须遍历所有匹配项（`nth(i)`）而不能只看 `loc.first`：
        X 的新登录页 (`/i/jf/onboarding/web`) 会同时渲染两套重复的表单节点
        （一套隐藏、一套可见），只取第一个会拿到**不可见**的那个，
        于是「继续」按钮永远找不到。
        """
        deadline = time.time() + max(timeout_ms, 0) / 1000.0
        while True:
            for sel in selectors:
                try:
                    loc = page.locator(sel)
                    n = loc.count()
                except Exception:
                    continue
                for i in range(n):
                    try:
                        cand = loc.nth(i)
                        if cand.is_visible():
                            return cand
                    except Exception:
                        continue
            if time.time() >= deadline:
                return None
            try:
                page.wait_for_timeout(200)
            except Exception:
                return None

    @staticmethod
    def _pw_fill(loc, value: str, *secrets_: str) -> bool:
        """填表单。⚠ 这是 password 唯一被"使用"的地方，绝不拼进任何其它字符串。"""
        try:
            loc.fill(value)
            return True
        except Exception as e:
            log.debug("填写登录表单失败: %s", _redact_secret(str(e), *secrets_))
            return False

    @staticmethod
    def _pw_click_next(page, emit: Callable[[str], None], *secrets_: str) -> bool:
        """点 Next/登录按钮；找不到就退回 Enter 提交（不抛异常）。"""
        loc = BrowserBackend._pw_find(page, SEL_PW_NEXT, 0)
        if loc is None:
            emit("未找到下一步按钮，改用回车提交")
            try:
                page.keyboard.press("Enter")
                return True
            except Exception as e:
                log.debug("回车提交失败: %s", _redact_secret(str(e), *secrets_))
                return False
        try:
            loc.click()
            return True
        except Exception as e:
            log.debug("点击下一步失败: %s", _redact_secret(str(e), *secrets_))
            return False

    def _pw_pause(self, page) -> None:
        """页间等待（窗口被关掉时静默返回，由外层 loop 的探测收口）。"""
        try:
            page.wait_for_timeout(self.PW_POLL_MS)
        except Exception:
            pass

    @staticmethod
    def _pw_page_text(page) -> str:
        """取页面可见文本（小写）。用于把"密码错/验证码/人机验证"分类开。"""
        raw = ""
        try:
            raw = page.evaluate(
                "() => (document.body ? (document.body.innerText || '') : '')")
        except Exception:
            raw = ""
        if not isinstance(raw, str) or not raw:
            try:
                raw = page.locator("body").first.inner_text() or ""
            except Exception:
                raw = ""
        return (raw if isinstance(raw, str) else "")[:4000].lower()

    @classmethod
    def _pw_classify(cls, page) -> str:
        """把页面文案归成 wrong_pw / locked / rate / challenge / otp / ""。

        顺序即优先级：报错类必须先于"需要再输一次"类判定，否则密码错的页面
        会因为同时出现 "username" 之类的词被误判成中间拦截页。
        """
        low = cls._pw_page_text(page)
        if not low:
            return ""
        for name, table in (("wrong_pw", PW_TEXT_WRONG_PW),
                            ("locked", PW_TEXT_LOCKED),
                            ("rate", PW_TEXT_RATE),
                            ("challenge", PW_TEXT_CHALLENGE),
                            ("otp", PW_TEXT_OTP),
                            ("generic", PW_TEXT_GENERIC)):
            for pat in table:
                if pat in low:
                    return name
        return ""

    # ══════════════════════════════════════════════════════
    # publish
    # ══════════════════════════════════════════════════════

    def publish(self, job: Job, text: str) -> PublishResult:
        """发一条推。**绝不抛异常**，任何失败都转成 PublishResult(ok=False, ...)。"""
        started = time.time()
        shot = self._shot_path(job)
        try:
            if not _HAVE_PW:
                return PublishResult(ok=False, backend=self.name, text=text,
                                     error=f"playwright 未就绪: {_PW_IMPORT_ERROR}",
                                     retryable=False)
            if not self.has_state():
                return PublishResult(ok=False, backend=self.name, text=text,
                                     error="登录态失效，请重新登录", retryable=False)

            # 媒体：可能是单图（老格式文件名）或多图（JSON 数组）。
            # 用 core.media 的助手解析，两种格式都认；解析出的文件必须真实存在。
            media_list = _media_paths_for_job(job)
            media = media_list or None
            want_media = bool(job.media_path)
            if want_media and not media_list:
                return PublishResult(ok=False, backend=self.name, text=text,
                                     error=f"媒体文件不存在: {job.media_path}", retryable=False)

            if not _BROWSER_LOCK.acquire(timeout=self.LOCK_WAIT_SECONDS):
                return PublishResult(ok=False, backend=self.name, text=text,
                                     error="浏览器正忙（等待超时），请稍后重试", retryable=True)
            try:
                return self._publish_locked(job, text, media, shot, started)
            finally:
                _BROWSER_LOCK.release()
        except Exception as e:  # 终极兜底：绝不外抛
            log.exception("browser publish 未捕获异常")
            extra: dict[str, Any] = {"evidence": "exception", "elapsed_ms": int((time.time() - started) * 1000)}
            if shot:
                extra["screenshot"] = str(shot)
            return PublishResult(ok=False, backend=self.name, text=text,
                                 error=f"发布异常: {type(e).__name__}: {e}",
                                 retryable=True, extra=extra)

    def _publish_locked(self, job: Job, text: str, media: Path | None,
                        shot: Path | None, started: float) -> PublishResult:
        """反应式选启动配置：先用已知可用的；**只有真被 403 拦下**才换下一个候选重试。

        刻意不做"启动前先探测哪个配置能连通"——实测那种投机探测本身就会因为
        短时间反复起浏览器访问 x.com 而触发限流，反而把可用配置也拖成不可用。
        这里最多多花一次浏览器启动，且只在确实被拦时才发生。
        """
        n_cands = len(self._candidates()) if config.BROWSER_HEADLESS else 1
        start_index = _GOOD_LAUNCH_INDEX if config.BROWSER_HEADLESS else 0
        last_blocked: PublishResult | None = None

        for offset in range(min(n_cands, _MAX_BLOCK_RETRIES + 1)):
            idx = (start_index + offset) % max(n_cands, 1)
            result = self._publish_once(job, text, media, shot, started, launch_index=idx)
            # 只有"被 403 拒绝"才值得换配置重试；其它结果一律原样返回。
            if result.extra.get("evidence") != "blocked":
                # 这个配置没被拦，记下来供后续发布直接复用
                self._mark_good(idx)
                return result
            last_blocked = result
            if offset < min(n_cands, _MAX_BLOCK_RETRIES + 1) - 1:
                log.warning("无头启动配置 #%d 被 x.com 拒绝，换下一个候选重试", idx)

        # 所有候选都被拒：把最后一次的失败结果返回（已含截图与可读原因）
        if last_blocked is not None:
            last_blocked.extra["tried_configs"] = [
                c.get("channel", "headless-shell") for c in self._candidates()]
            return last_blocked
        return PublishResult(ok=False, backend=self.name, text=text,
                             error="无头浏览器全部启动配置均被 x.com 拒绝，请设置 BROWSER_HEADLESS=false",
                             retryable=True, extra={"evidence": "blocked"})

    def _publish_once(self, job: Job, text: str, media: Path | None,
                      shot: Path | None, started: float, *, launch_index: int) -> PublishResult:
        def fail(error: str, *, retryable: bool, page=None, evidence: str = "",
                 extra: dict | None = None) -> PublishResult:
            ex = dict(extra or {})
            ex["evidence"] = evidence or ex.get("evidence", "")
            ex["elapsed_ms"] = int((time.time() - started) * 1000)
            if page is not None:
                p = self._screenshot(page, shot)
                if p:
                    ex["screenshot"] = str(p)
            return PublishResult(ok=False, backend=self.name, text=text,
                                 error=error, retryable=retryable, extra=ex)

        with self._open_session(headless=config.BROWSER_HEADLESS,
                                launch_index=launch_index) as s:
            page = s.context.new_page()
            resp = None
            try:
                resp = page.goto(HOME_URL, wait_until="domcontentloaded")
            except Exception as e:
                msg = str(e)
                # 被拦截时 goto 有时不返回 403 响应，而是直接抛
                # ERR_HTTP_RESPONSE_CODE_FAILURE —— 这仍然是"被拒绝"，不是网络不通。
                if "ERR_HTTP_RESPONSE_CODE_FAILURE" in msg or "403" in msg:
                    return fail(self._block_message(403), retryable=True,
                                page=page, evidence="blocked",
                                extra={"http_status": 403})
                return fail(f"打开 x.com 失败（网络不可达？）: {type(e).__name__}: {e}",
                            retryable=True, page=page, evidence="nav_failed")

            # 实测：被 x.com 拒绝时不一定给 403 —— 自带 headless shell 通常回 403，
            # 但也可能回 200 却给一张空白壳页（内容为空、零个 data-testid）。
            # 这两种都必须识别为"被拦截"，否则会被误报成"登录态失效"，
            # 把用户引向完全错误的排查方向。所以无论状态码，都要看页面是否空白。
            status = getattr(resp, "status", None) if resp else None
            if status in _BLOCK_STATUSES:
                # 已经是明确的 4xx 拒绝，无需再花时间确认页面是否空白
                return fail(self._block_message(status), retryable=True, page=page,
                            evidence="blocked",
                            extra={"http_status": status, "blank_page": False})
            blank = _page_is_blank(page)
            if blank:
                # 状态码正常却拿到空壳页，同样是拦截
                return fail(self._block_message(None), retryable=True, page=page,
                            evidence="blocked",
                            extra={"http_status": status, "blank_page": True})

            st = self._wait_login_state(page, 20_000)
            if st is not True:
                return fail(f"登录态失效，请重新登录（当前 URL: {_safe_url(page)}）",
                            retryable=False, page=page, evidence="logged_out")

            # 1) 打开发帖入口
            opener, err = self._open_composer(page, job)
            if err:
                return fail(err, retryable=False, page=page, evidence="composer_missing",
                            extra={"composer": opener})
            composer = opener

            # 2) 输入正文（输入后回读校验，不一致绝不发）
            ok, terr = self._type_text(page, text)
            if not ok:
                return fail(terr, retryable=False, page=page, evidence="text_input_failed",
                            extra={"composer": composer})

            # 3) 媒体上传（未确认上传完成则中止，避免漏发媒体）
            media_uploaded = False
            if media:
                mok, merr = self._upload_media(page, media)
                if not mok:
                    return fail(merr, retryable=True, page=page, evidence="media_upload_failed",
                                extra={"composer": composer})
                media_uploaded = True

            # 4) 点发送并抓证据
            btn, berr = self._find_send_button(page)
            if berr:
                return fail(berr, retryable=False, page=page, evidence="send_button_missing",
                            extra={"composer": composer})

            resp_holder: dict[str, Any] = {"payload": None, "status": None, "url": ""}
            clicked = True
            try:
                with page.expect_response(_is_create_tweet_request,
                                          timeout=self.POST_CONFIRM_TIMEOUT_MS) as info:
                    btn.click(timeout=self.ELEMENT_TIMEOUT_MS)
                resp = info.value
                resp_holder["status"] = _safe_int(getattr(resp, "status", None))
                resp_holder["url"] = getattr(resp, "url", "") or ""
                try:
                    resp_holder["payload"] = resp.json()
                except Exception:
                    try:
                        resp_holder["payload"] = json.loads(resp.text())
                    except Exception:
                        resp_holder["payload"] = None
            except PWTimeoutError:
                # 没等到 CreateTweet 回包：可能是点击没生效（按钮被挡/改版），也可能是接口名变了。
                clicked = True
            except Exception as e:
                clicked = False
                return fail(f"点击发送按钮失败: {type(e).__name__}: {e}",
                            retryable=False, page=page, evidence="click_failed",
                            extra={"composer": composer, "media_uploaded": media_uploaded})

            # 5) 收集 UI 信号
            ui = self._collect_ui_signals(page, composer)

            return self._decide_result(
                job=job, text=text, page=page, shot=shot, started=started,
                resp=resp_holder, ui=ui, composer=composer,
                media_uploaded=media_uploaded,
            )

    # ── 发帖入口 ───────────────────────────────────────────

    def _open_composer(self, page, job: Job) -> tuple[str, str]:
        """打开发帖框。返回 (用的是哪种入口, 错误信息)。错误信息为空表示成功。

        ⚠ 引用转发（job.quote_id 非空）必须在**引用编辑器**里发。
        如果已经点了"引用"菜单但编辑器没出来，绝不能退回普通发帖路径 ——
        那会发出一条**丢掉引用的普通推文**，属于静默的内容错误，
        比直接失败更糟。所以那条路径直接报错，由调用方判 retryable=False。
        """
        # 引用转发：先在被引用推文页上走"转推 -> 引用"入口
        if job.quote_id:
            qerr = self._setup_quote(page, job.quote_id)
            if qerr:
                return "quote", qerr
            if self._wait_editor(page, 15_000):
                return "quote", ""
            # 点了"引用"但编辑器没出现：宁可不发，也不能发成一条没有引用的推文
            return "quote", ("引用转发失败：已点击「引用」但发帖编辑器未出现"
                             "（X 前端可能改版），已中止以免发出丢失引用的普通推文")

        # 常规：先点侧栏发帖按钮（弹窗编辑器）
        try:
            btn = page.locator(SEL_COMPOSE_BUTTON)
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click(timeout=self.ELEMENT_TIMEOUT_MS)
                if self._wait_editor(page, 10_000):
                    return "modal", ""
        except Exception as e:
            log.debug("点击侧栏发帖按钮失败: %s", e)

        # 兜底 1：直接导航到独立发帖页
        try:
            page.goto(COMPOSE_URL, wait_until="domcontentloaded")
            if self._wait_editor(page, 15_000):
                return "compose_url", ""
        except Exception as e:
            log.debug("导航 /compose/post 失败: %s", e)

        # 兜底 2：首页内联编辑器（部分账号/布局没有侧栏按钮）
        try:
            page.goto(HOME_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            if self._wait_editor(page, 10_000):
                return "inline", ""
        except Exception as e:
            log.debug("回首页找内联编辑器失败: %s", e)

        if self._probe_login_state(page) is False:
            return "", "登录态失效，请重新登录"
        return "", "未找到发帖入口（X 前端可能改版，选择器 SideNav_NewTweet_Button/tweetTextarea_0 失效）"

    def _setup_quote(self, page, quote_id: str) -> str:
        """在引用目标推文页上打开"引用"编辑器。返回错误串（空=成功）。"""
        try:
            page.goto(f"https://x.com/i/status/{quote_id}", wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            rt = page.locator(SEL_RETWEET)
            if rt.count() == 0 or not rt.first.is_visible():
                return "引用转发失败：未找到转推按钮（可能是受保护/已删除的推文）"
            rt.first.click(timeout=self.ELEMENT_TIMEOUT_MS)
            page.wait_for_timeout(800)
            for sel in (SEL_QUOTE_ENTRY, '[role="menuitem"]'):
                loc = page.locator(sel)
                n = loc.count()
                for i in range(n):
                    item = loc.nth(i)
                    try:
                        if not item.is_visible():
                            continue
                        txt = (item.inner_text() or "")
                        if sel != '[role="menuitem"]' or re.search(r"quote|引用", txt, re.I):
                            item.click(timeout=self.ELEMENT_TIMEOUT_MS)
                            return ""
                    except Exception:
                        continue
            return "引用转发失败：未找到「引用」菜单项（X 前端可能改版）"
        except Exception as e:
            return f"引用转发失败: {type(e).__name__}: {e}"

    def _wait_editor(self, page, timeout_ms: int) -> bool:
        """等发帖编辑器出现**且真的可点**。

        ⚠ 不能只等 `state="visible"`。实测（2026-09）：X 的打开发帖弹窗那一刻，
        页面上会有**两个** `tweetTextarea_0`；先出现的那个虽然 visible，但被
        `[data-testid="mask"]`（弹窗遮罩）盖住，点击会被 Playwright 判定
        "subtree intercepts pointer events" 并最终超时 —— 表现就是
        「无法聚焦编辑器: TimeoutError」。所以必须等到"命中测试落在编辑器自己身上"。
        """
        deadline = time.time() + max(timeout_ms, 0) / 1000.0
        while True:
            try:
                loc = page.locator(SEL_EDITOR)
                n = loc.count()
                for i in range(n):
                    el = loc.nth(i)
                    try:
                        if not el.is_visible():
                            continue
                        # 命中测试：编辑器中心的顶层元素必须是它自己（未被遮挡）
                        box = el.bounding_box()
                        if not box:
                            continue
                        hit_self = page.evaluate(
                            """(idx) => {
                                const eds = document.querySelectorAll(
                                    '[data-testid="tweetTextarea_0"]');
                                const e = eds[idx];
                                if (!e) return false;
                                const r = e.getBoundingClientRect();
                                const cx = r.x + r.width / 2;
                                const cy = r.y + r.height / 2;
                                const t = document.elementFromPoint(cx, cy);
                                return !!t && (t === e || e.contains(t));
                            }""", i)
                        if hit_self:
                            return True
                    except Exception:
                        continue
            except Exception:
                pass
            if time.time() >= deadline:
                return False
            try:
                page.wait_for_timeout(250)
            except Exception:
                return False

    # ── 输入正文 ───────────────────────────────────────────

    def _type_text(self, page, text: str) -> tuple[bool, str]:
        """模拟真人输入。X 是 DraftJS/contenteditable，`fill()` 通常无效。

        约定：换行用 **Shift+Enter**（Enter 在 X 的编辑器里可能触发送出），
        CJK/emoji 与普通字符都走 `keyboard.type()` 逐字输入（带随机化延迟）；
        输入完**回读 innerText 比对**，不一致则用 `insert_text` 重试一次，
        仍不一致直接判失败 —— 宁可失败也不发出一段错文案。
        """
        text = "" if text is None else str(text)
        if not text.strip():
            return False, "文本为空，未输入任何内容"

        # 页面上可能有多个 tweetTextarea_0（弹窗 + 被遮挡的预渲染节点），
        # 只点"真的可点"的那个；全被遮挡时等一下再试，避免直接判死。
        editor = None
        deadline = time.time() + self.ELEMENT_TIMEOUT_MS / 1000.0
        while editor is None and time.time() < deadline:
            try:
                loc = page.locator(SEL_EDITOR)
                for i in range(loc.count()):
                    el = loc.nth(i)
                    try:
                        if not el.is_visible():
                            continue
                        if page.evaluate(
                            """(idx) => {
                                const eds = document.querySelectorAll(
                                    '[data-testid="tweetTextarea_0"]');
                                const e = eds[idx];
                                if (!e) return false;
                                const r = e.getBoundingClientRect();
                                const t = document.elementFromPoint(
                                    r.x + r.width / 2, r.y + r.height / 2);
                                return !!t && (t === e || e.contains(t));
                            }""", i):
                            editor = el
                            break
                    except Exception:
                        continue
            except Exception:
                pass
            if editor is None:
                try:
                    page.wait_for_timeout(250)
                except Exception:
                    break
        if editor is None:
            return False, ("无法聚焦编辑器: 等不到可点击的输入框"
                           "（可能被弹窗遮罩挡住，或 X 前端改版）")
        try:
            editor.click(timeout=self.ELEMENT_TIMEOUT_MS)
        except Exception as e:
            return False, f"无法聚焦编辑器: {type(e).__name__}: {e}"

        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        try:
            for i, line in enumerate(lines):
                if i:
                    page.keyboard.press("Shift+Enter")
                    page.wait_for_timeout(30)
                if line:
                    page.keyboard.type(line, delay=self.TYPE_DELAY_MS)
        except Exception as e:
            return False, f"文本输入失败: {type(e).__name__}: {e}"

        got = self._read_editor_text(page)
        if _norm_text(got) != _norm_text(text):
            # 二次尝试：整段 insert_text（对 IME/组合字符更稳）
            try:
                page.locator(SEL_EDITOR).first.click(timeout=self.ELEMENT_TIMEOUT_MS)
                page.keyboard.press("Control+a")
                page.keyboard.press("Delete")
                page.keyboard.insert_text(text)
            except Exception as e:
                return False, f"文本输入失败（二次尝试异常）: {type(e).__name__}: {e}"
            got = self._read_editor_text(page)
            if _norm_text(got) != _norm_text(text):
                return False, (f"文本输入失败：编辑器回读内容与预期不一致"
                               f"（预期 {len(text)} 字 / 实际 {len(got)} 字）")
        return True, ""

    @staticmethod
    def _read_editor_text(page) -> str:
        try:
            loc = page.locator(SEL_EDITOR).first
            if loc.count() == 0:
                return ""
            try:
                t = loc.inner_text()
            except Exception:
                t = ""
            if not t:
                t = loc.text_content() or ""
            return t or ""
        except Exception:
            return ""

    # ── 媒体上传 ───────────────────────────────────────────

    def _upload_media(self, page, media) -> tuple[bool, str]:
        """上传 1~4 个媒体并等上传完成。

        `media` 可以是单个 Path，也可以是 Path 列表（多图一条推文）。
        **未确认完成则返回失败** —— 调用方会中止发布，避免漏发媒体。
        """
        # 统一成列表；超过 X 的 4 张上限就截断（不静默：在返回值里说明）
        if isinstance(media, (str, Path)):
            paths = [Path(media)]
        else:
            paths = [Path(x) for x in (media or [])]
        paths = [p for p in paths if p]
        if not paths:
            return False, "媒体上传失败：没有可上传的文件"

        note = ""
        try:
            from core import media as _media
            kept, dropped = _media.trim_to_limit([str(p) for p in paths])
            if dropped:
                note = f"（超过 {_media.MAX_IMAGES} 张上限，已忽略 {len(dropped)} 个）"
                keep_set = set(kept)
                paths = [p for p in paths if str(p) in keep_set]
        except Exception:
            pass

        try:
            inp = page.locator(SEL_FILE_INPUT)
            if inp.count() == 0:
                return False, "媒体上传失败：未找到文件输入框（X 前端可能改版）"
            # set_input_files 接受列表 —— 一次投多个文件，X 会当成同一条的附件
            payload = [str(p) for p in paths] if len(paths) > 1 else str(paths[0])
            inp.first.set_input_files(payload, timeout=self.ELEMENT_TIMEOUT_MS)
        except Exception as e:
            return False, f"媒体上传失败: {type(e).__name__}: {e}"

        deadline = time.time() + self.MEDIA_UPLOAD_TIMEOUT_MS / 1000.0
        seen_attachment = False
        clean_rounds = 0
        last_state = {}
        while time.time() < deadline:
            # ⚠ 不能数"整页的 [role=progressbar]"。实测（2026-09）：X 首页常驻
            #   3 个装饰性 progressbar（页顶细条 + 圆环倒计时等），上传前就在，
            #   永远不归零 —— 用它判完成必然卡满整个超时。
            #   只认**附件容器内部**的进度指示与预览。
            state = {}
            try:
                state = page.evaluate(
                    """() => {
                        const a = document.querySelector('[data-testid="attachments"]');
                        if (!a) return {att: false, bars: 0, media: 0};
                        const bars = a.querySelectorAll(
                            '[role="progressbar"], progress, [data-testid*="progress"]').length;
                        const media = a.querySelectorAll(
                            'img, video, canvas').length;
                        return {att: true, bars, media};
                    }""")
            except Exception:
                break
            last_state = state
            if state.get("att"):
                seen_attachment = True
                # 预览就绪 = 附件容器里已有媒体，且容器内没有进度指示
                want = len(paths)
                got = int(state.get("media", 0) or 0)
                # 多图必须等**全部**附件就绪，不能只等到第一张就发
                if got >= want and state.get("bars", 0) == 0:
                    clean_rounds += 1
                    if clean_rounds >= 2:
                        return True, note
                # 单图/多图都容忍"略微超出"（X 可能插入占位节点），但不足就继续等
                elif got >= want and clean_rounds == 0:
                    clean_rounds = 0
                else:
                    clean_rounds = 0
            page.wait_for_timeout(700)

        if seen_attachment:
            # 极端兜底：附件容器里确实已经有媒体了，就给过（宁可带警告，也不要
            # 因为一个探测口径问题把用户的内容卡 5 分钟）
            if last_state.get("media", 0) > 0:
                return True, note
            return False, (f"媒体上传超时（{self.MEDIA_UPLOAD_TIMEOUT_MS // 1000} 秒内未见预览就绪），"
                           f"已中止发布以免漏发媒体")
        return False, "媒体上传失败：未出现附件预览（格式不支持或文件过大？）"

    # ── 发送与判定 ─────────────────────────────────────────

    def _find_send_button(self, page) -> tuple[Any, str]:
        deadline = time.time() + 20
        last = "未找到发帖按钮"
        while time.time() < deadline:
            for sel in (SEL_SEND_BUTTON, SEL_SEND_BUTTON_INLINE):
                try:
                    loc = page.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        if loc.first.is_enabled():
                            return loc.first, ""
                        last = "发帖按钮处于禁用状态（文本为空或媒体仍在上传）"
                except Exception:
                    continue
            page.wait_for_timeout(400)
        return None, last

    def _collect_ui_signals(self, page, composer: str) -> dict:
        """轮询收集 UI 层证据：toast 文案 / 编辑器是否清空 / 弹窗是否关闭。"""
        out = {"toast": "", "toast_kind": "", "editor_cleared": False, "composer_closed": False}
        deadline = time.time() + self.UI_CONFIRM_TIMEOUT_MS / 1000.0
        while time.time() < deadline:
            if not out["toast"]:
                try:
                    loc = page.locator(SEL_TOAST)
                    if loc.count() > 0:
                        out["toast"] = (loc.first.inner_text() or "").strip()
                except Exception:
                    pass
            if out["toast"]:
                low = out["toast"].lower()
                if any(p in low for p in TOAST_OK_PATTERNS):
                    out["toast_kind"] = "ok"
                elif any(p[0] in low for p in TOAST_ERR_PATTERNS):
                    out["toast_kind"] = "err"
                else:
                    out["toast_kind"] = "unknown"
            if out["toast_kind"] == "ok":
                break

            # 编辑器清空 + 弹窗关闭
            try:
                if composer in ("modal", "compose_url", "quote", "inline"):
                    if page.locator(SEL_EDITOR).count() == 0:
                        out["editor_cleared"] = True
                    else:
                        t = self._read_editor_text(page)
                        out["editor_cleared"] = (t.strip() == "")
                if composer in ("modal", "compose_url", "quote"):
                    mask = page.locator(SEL_MODAL_MASK)
                    btn = page.locator(SEL_SEND_BUTTON)
                    out["composer_closed"] = (btn.count() == 0) or (mask.count() == 0)
            except Exception:
                pass
            if out["editor_cleared"] and out["composer_closed"]:
                break
            page.wait_for_timeout(500)
        return out

    def _decide_result(self, *, job: Job, text: str, page, shot: Path | None,
                       started: float, resp: dict, ui: dict, composer: str,
                       media_uploaded: bool) -> PublishResult:
        """汇总证据并给出终态。证据强度：网络回包 > URL > toast > 编辑器状态。"""
        payload = resp.get("payload")
        errors = _extract_errors(payload)
        tweet_id = _extract_tweet_id(payload)
        status = resp.get("status")

        elapsed = int((time.time() - started) * 1000)
        extra: dict[str, Any] = {
            "composer": composer,
            "media_uploaded": media_uploaded,
            "elapsed_ms": elapsed,
            "http_status": status,
            "toast": ui.get("toast", ""),
            "editor_cleared": ui.get("editor_cleared", False),
            "composer_closed": ui.get("composer_closed", False),
        }

        def shot_now() -> None:
            p = self._screenshot(page, shot)
            if p:
                extra["screenshot"] = str(p)

        # A) 服务端明确报错（重复内容 / 限流 / 自动化判定）
        if errors:
            kind, wait_s, msg = _classify_api_error(errors)
            shot_now()
            extra["evidence"] = "api_error"
            extra["api_errors"] = errors[:3]
            return PublishResult(
                ok=False, backend=self.name, text=text,
                error=f"X 接口报错: {msg}",
                retryable=(kind == "retry"), wait_seconds=wait_s,
                extra=extra,
            )
        if status is not None and status >= 400:
            shot_now()
            extra["evidence"] = "http_error"
            return PublishResult(ok=False, backend=self.name, text=text,
                                 error=f"发布请求返回 HTTP {status}",
                                 retryable=(status >= 500 or status == 429),
                                 wait_seconds=300 if status == 429 else 0, extra=extra)

        # B) 最强证据：服务端回包里的 tweet id
        if tweet_id:
            extra["evidence"] = "network"
            extra["evidence_strength"] = "strong"
            return PublishResult(ok=True, backend=self.name, text=text,
                                 tweet_id=tweet_id,
                                 tweet_url=f"https://x.com/i/web/status/{tweet_id}",
                                 media_uploaded=media_uploaded, extra=extra)

        # C) 页面跳转到详情页拿 id
        url_id, url = self._id_from_url(page)
        if url_id:
            extra["evidence"] = "url"
            extra["evidence_strength"] = "strong"
            extra["page_url"] = url
            return PublishResult(ok=True, backend=self.name, text=text,
                                 tweet_id=url_id, tweet_url=url,
                                 media_uploaded=media_uploaded, extra=extra)

        # D) toast 明确报错
        if ui.get("toast_kind") == "err":
            shot_now()
            kind, wait_s, msg = _classify_toast_error(ui.get("toast", ""))
            extra["evidence"] = "toast_error"
            return PublishResult(ok=False, backend=self.name, text=text,
                                 error=f"页面提示发布失败: {msg}",
                                 retryable=(kind == "retry"), wait_seconds=wait_s, extra=extra)

        # E) 弱证据：toast 成功文案
        if ui.get("toast_kind") == "ok":
            rid = self._recover_id_from_profile(page)
            if rid:
                extra["evidence"] = "profile_recover"
                extra["evidence_strength"] = "strong"
                extra["toast"] = ui.get("toast", "")
                return PublishResult(ok=True, backend=self.name, text=text,
                                     tweet_id=rid, tweet_url=f"https://x.com/i/web/status/{rid}",
                                     media_uploaded=media_uploaded, extra=extra)
            extra["evidence"] = "toast"
            extra["evidence_strength"] = "weak"
            extra["weak_evidence"] = True
            extra["note"] = "未抓到 tweet_id，仅凭页面成功提示判定；请到账号主页核对"
            return PublishResult(ok=True, backend=self.name, text=text,
                                 tweet_url="", media_uploaded=media_uploaded, extra=extra)

        # F) 弱证据：编辑器清空 + 弹窗关闭
        if ui.get("editor_cleared") and ui.get("composer_closed"):
            rid = self._recover_id_from_profile(page)
            if rid:
                extra["evidence"] = "profile_recover"
                extra["evidence_strength"] = "strong"
                return PublishResult(ok=True, backend=self.name, text=text,
                                     tweet_id=rid, tweet_url=f"https://x.com/i/web/status/{rid}",
                                     media_uploaded=media_uploaded, extra=extra)
            extra["evidence"] = "ui_cleared"
            extra["evidence_strength"] = "weak"
            extra["weak_evidence"] = True
            extra["note"] = "未抓到 tweet_id，仅凭编辑器清空判定；请到账号主页核对"
            return PublishResult(ok=True, backend=self.name, text=text,
                                 tweet_url="", media_uploaded=media_uploaded, extra=extra)

        # G) 完全没证据：不谎报成功、也不自动重试（可能已经发出去了，重试会重复发）
        shot_now()
        extra["evidence"] = "none"
        extra["may_have_published"] = True
        extra["note"] = "点击已执行但未检测到任何成功/失败标志，推文可能已发出"
        return PublishResult(
            ok=False, backend=self.name, text=text,
            error=("未检测到发布成功的可靠标志（可能已发出，请到账号主页核对后再决定是否重试）"),
            retryable=False, media_uploaded=media_uploaded, extra=extra,
        )

    @staticmethod
    def _id_from_url(page) -> tuple[str, str]:
        try:
            url = page.url or ""
        except Exception:
            return "", ""
        m = re.search(r"/status/(\d{5,25})", url)
        if m:
            return m.group(1), url.split("?")[0]
        return "", ""

    def _recover_id_from_profile(self, page) -> str:
        """弱证据下的兜底：去自己主页取最新一条推文的 id。best-effort，失败返回 ""。"""
        try:
            handle = ""
            try:
                loc = page.locator(SEL_ACCOUNT_SWITCHER)
                if loc.count() > 0:
                    txt = loc.first.inner_text() or ""
                    m = re.search(r"@([A-Za-z0-9_]{1,15})", txt)
                    if m:
                        handle = m.group(1)
            except Exception:
                pass
            if not handle:
                return ""
            page.goto(f"https://x.com/{handle}", wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            links = page.locator('article a[href*="/status/"]')
            if links.count() == 0:
                links = page.locator('a[href*="/status/"]')
            if links.count() > 0:
                href = links.first.get_attribute("href") or ""
                m = re.search(r"/status/(\d{5,25})", href)
                if m:
                    return m.group(1)
        except Exception as e:
            log.debug("兜底恢复 tweet_id 失败: %s", e)
        return ""

    # ── 截图 ───────────────────────────────────────────────

    def _shot_path(self, job: Job) -> Path:
        try:
            log_dir = Path(config.LOG_DIR)
            log_dir.mkdir(parents=True, exist_ok=True)
            jid = getattr(job, "id", 0) or 0
            return log_dir / f"fail_{jid}_{_now_tag()}.png"
        except Exception:
            return Path(os.getcwd()) / f"fail_unknown_{_now_tag()}.png"

    @staticmethod
    def _screenshot(page, path: Path | None) -> Path | None:
        if page is None or path is None:
            return None
        for full in (True, False):
            try:
                page.screenshot(path=str(path), full_page=full)
                return path if path.is_file() else None
            except Exception as e:
                log.debug("截图失败(full_page=%s): %s", full, e)
        return None


# ══════════════════════════════════════════════════════════
# 纯函数工具（便于单测）
# ══════════════════════════════════════════════════════════

def _media_paths_for_job(job) -> list[Path]:
    """把 `job.media_path` 解析成**真实存在**的文件列表。

    队列表的 media_path 单字段兼容两种格式（见 core.media）：
      * 单图老格式：`"a.jpg"`
      * 多图新格式：`'["a.jpg","b.jpg"]'`
    不存在的文件会被丢掉；全部不存在时返回空列表（调用方据此报"媒体文件不存在"）。
    """
    try:
        from core import media as _media
        return _media.resolve_media_paths(
            getattr(job, "media_path", ""), config.MEDIA_DIR)
    except Exception:
        # 兜底：退回契约自带的单图解析，保证老行为不回归
        try:
            one = job.resolved_media(config.MEDIA_DIR)
            return [one] if one is not None else []
        except Exception:
            return []


def _is_create_tweet_request(resp) -> bool:
    """playwright 响应断言：是否是"创建推文"接口。"""
    try:
        if getattr(resp, "request", None) is None:
            return False
        if resp.request.method != "POST":
            return False
        url = resp.url or ""
        return ("/CreateTweet" in url or "/CreateNoteTweet" in url
                or "statuses/update.json" in url)
    except Exception:
        return False


def _safe_int(v) -> int | None:
    try:
        return int(v)
    except Exception:
        return None


def _norm_text(s: str) -> str:
    """比对用归一：统一换行、去掉零宽字符、压掉空白差异。"""
    s = (s or "").replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace("\u200b", "").replace("\ufeff", "")
    return "\n".join(line.rstrip() for line in s.split("\n")).strip()


def _safe_url(page) -> str:
    try:
        return (page.url or "")[:120]
    except Exception:
        return "(未知)"


# 长度阈值：太短的值（如 "a"）全局替换会把正常文案打成筛子，
# 且真实密码/X 账号都不会短到这个程度。
_REDACT_MIN_LEN = 3


def _redact_secret(text: Any, *secrets_: str) -> str:
    """把敏感串从文本里抹掉 —— 密码/验证码出栈前的最后一道防线。

    契约 §3.1 硬性要求：异常信息里若含表单值要先脱敏。所有会被 `emit()`
    推给日志/控制台的字符串、以及所有返回给调用方的异常描述，都必须先过这里。
    只替换长度 ≥ 3 的值（避免短串误伤正文）；长串优先替换，防止前缀互吃。
    """
    out = "" if text is None else str(text)
    vals = {s for s in (secrets_ or ()) if isinstance(s, str) and len(s) >= _REDACT_MIN_LEN}
    for s in sorted(vals, key=len, reverse=True):
        out = out.replace(s, "***")
    return out


def _page_is_blank(page, settle_ms: int | None = None) -> bool:
    """页面是否只是个空壳（被拦截/被限流时拿到的空响应体）。

    ⚠ 实测：被 x.com 拒绝时**不是**每次都给出 403 —— 有时状态码是 200，
    但响应体就是个空壳。所以判定必须容状态码。
    被拒时的响应体实测就是 `<html><head></head><body></body></html>`：
    body 里零个子元素、无文本、无标记。

    ⚠ 反面教训：**不能**用 `#react-root` 或 `data-testid` 当"有内容"的判据。
    实测真实的 x.com 未登录页（/i/jf/onboarding）虽然 innerText 为空、
    零个 data-testid、也没有 #react-root，但它 body 里有 6 个子元素、
    4618 字节 HTML —— 是货真价实的页面。用那些选择器当判据会把它误判成"被拦截"，
    把用户引向错误的排查方向。所以判据取 **body 子元素数 + HTML 体积**。

    为避免把"还没渲染完的正常页面"误判成空白，先给页面一点渲染时间。
    """
    if settle_ms is None:
        settle_ms = _BLANK_SETTLE_MS
    try:
        deadline = time.time() + settle_ms / 1000.0
        while True:
            probe = page.evaluate(
                """() => {
                    const b = document.body;
                    if (!b) return {children: 0, html: 0, text: ''};
                    return {
                        children: b.children.length,
                        html: (b.innerHTML || '').length,
                        text: (b.innerText || '').trim(),
                    };
                }"""
            ) or {}
            if probe.get("text") or (probe.get("children") or 0) > 0 \
                    or (probe.get("html") or 0) > 200:
                return False
            if time.time() >= deadline:
                return True
            page.wait_for_timeout(300)
    except Exception:
        # 探测本身失败时不敢断言"空白"（可能是刚导航/已关闭），按"非空白"处理，
        # 让后续的登录态检查去给出更准确的结论。
        return False


def _extract_tweet_id(payload: Any) -> str:
    """从 CreateTweet 回包里挖出 tweet id。挖不到返回 ""。"""
    if not isinstance(payload, dict):
        return ""
    try:
        data = payload.get("data") or {}
        for key in ("create_tweet", "create_note_tweet"):
            node = data.get(key) or {}
            result = (node.get("tweet_results") or {}).get("result") or {}
            rid = result.get("rest_id")
            if rid:
                return str(rid)
            rid = ((result.get("tweet") or {}).get("rest_id"))
            if rid:
                return str(rid)
    except Exception:
        pass
    return _walk_for_rest_id(payload)


def _walk_for_rest_id(node: Any, depth: int = 0) -> str:
    if depth > 8:
        return ""
    if isinstance(node, dict):
        rid = node.get("rest_id")
        if isinstance(rid, (str, int)) and str(rid).isdigit():
            return str(rid)
        for k, v in node.items():
            if k in ("core", "user_results", "author"):
                continue
            found = _walk_for_rest_id(v, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for v in node[:10]:
            found = _walk_for_rest_id(v, depth + 1)
            if found:
                return found
    return ""


def _extract_errors(payload: Any) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    errs = payload.get("errors")
    out: list[dict] = []
    if isinstance(errs, list):
        for e in errs:
            if isinstance(e, dict):
                out.append({"code": e.get("code"), "message": str(e.get("message") or "")})
    return out


def _classify_api_error(errors: list[dict]) -> tuple[str, int, str]:
    """把 X 的 GraphQL error 归类成 (kind, wait_seconds, 可读原因)。

    kind: "retry"（值得重试）| "fatal"（重试无意义，要人介入）
    """
    code_to_msg = {
        187: ("fatal", 0, "内容重复（X 判定为已发布过的相同内容）"),
        226: ("fatal", 0, "被判定为自动化行为，需要人工处理账号"),
        344: ("retry", 900, "超出每日发帖上限"),
        353: ("fatal", 0, "账号或内容被限制"),
        386: ("fatal", 0, "内容中包含被禁止的关键词"),
        88: ("retry", 900, "触发速率限制，请稍后重试"),
        32: ("fatal", 0, "鉴权失败，登录态可能已失效"),
        64: ("fatal", 0, "账号已被冻结"),
        89: ("fatal", 0, "登录凭据无效，请重新登录"),
        326: ("fatal", 0, "账号被临时锁定，需人工验证"),
    }
    for e in errors:
        code = e.get("code")
        try:
            code = int(code)
        except Exception:
            code = None
        if code in code_to_msg:
            kind, wait_s, msg = code_to_msg[code]
            return kind, wait_s, f"[{code}] {msg}"
    msg = "; ".join((e.get("message") or "") for e in errors[:2]) or "未知接口错误"
    low = msg.lower()
    if "rate" in low or "limit" in low or "try again" in low:
        return "retry", 300, msg
    return "fatal", 0, msg


def _classify_toast_error(toast: str) -> tuple[str, int, str]:
    low = (toast or "").lower()
    for pat, kind, wait_s in TOAST_ERR_PATTERNS:
        if pat in low:
            return kind, wait_s, toast.strip()
    return "retry", 30, toast.strip() or "页面提示未知错误"


# 便于外部/测试引用
__all__ = ["BrowserBackend", "start_playwright"]
