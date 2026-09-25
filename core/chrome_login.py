#!/usr/bin/env python3
"""真实 Chrome 登录（CDP）—— 绕开 Playwright 的自动化指纹。

## 为什么需要它

`BrowserBackend.login()` 用 **Playwright 启动浏览器**，而 Playwright 启动时会带上
一串自动化开关（`--remote-debugging-pipe` / `--disable-features` /
`--no-startup-window` 等），x.com 的风控据此把整个会话判定为机器人。
表现是登录页弹「出了点问题」、URL 里出现 `prelude_gate`，
**用户手工点也没用** —— 被标记的是浏览器进程本身，不是操作方式。

本模块改用：

  1. 用**普通方式**启动系统真 Chrome（不带任何自动化开关）；
  2. 用户在窗口里像平常一样登录（两步验证、人机验证都能正常过）；
  3. 通过 Chrome **官方 CDP 调试接口**读取登录态；
  4. 导出成 Playwright 的 `storage_state.json`，之后照常无头发帖。

顺带绕开另一个坑：Chrome 127+ 的 **App-Bound Encryption** 使外部程序无法
离线解密 cookie 库（文件还被 Chrome 独占锁住）。那条限制只针对「离线解密」，
**问 Chrome 本人要**（CDP）完全可行 —— 这正是本模块的做法。

## 契约

对外只暴露 `login_via_chrome()`。**绝不抛异常**：所有失败路径都返回
`(False, "可读中文原因")`。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

X_LOGIN_URL = "https://x.com/login"

# 服务器（无桌面）上无法弹出浏览器窗口 —— 这是物理限制，不是 bug。
# 提前检测并给出**可操作的**提示，比让 Chrome 报一堆底层错误友好得多。
GUI_HINT = (
    "当前环境没有图形界面（无 DISPLAY / WAYLAND_DISPLAY），无法弹出登录窗口。\n"
    "请在**有桌面的机器**上登录一次，然后把生成的 storage_state.json 拷贝到本机：\n"
    "  · 登录命令：python tools/browser_login_chrome.py\n"
    "  · 文件位置：<项目>/data/browser/storage_state.json"
)


def has_display() -> bool:
    """当前环境是否可能有图形界面（用于给出可读提示）。"""
    if os.name == "nt":
        return True
    if sys.platform == "darwin":
        return True
    try:
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    except Exception:
        return False
AUTH_COOKIE = "auth_token"
# X 的关键认证 cookie：即使 CDP 偶发漏了 domain 也必须保住
AUTH_COOKIES = ("auth_token", "ct0", "twid")
CDP_WAIT_SECONDS = 30
POLL_SECONDS = 2.0

_CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)


# ══════════════════════════════════════════════════════════
# 定位 Chrome
# ══════════════════════════════════════════════════════════

def find_chrome() -> str:
    """定位系统安装的 Chrome。找不到返回空串（不抛异常）。"""
    try:
        env = (os.getenv("CHROME_PATH") or "").strip()
        if env and Path(env).exists():
            return env
        for c in _CHROME_CANDIDATES:
            if Path(c).exists():
                return c
        for name in ("chrome", "google-chrome", "google-chrome-stable", "chromium"):
            got = shutil.which(name)
            if got:
                return got
        # 兜底：Playwright 自带的 Chrome for Testing（指纹略差，但聊胜于无）
        local = os.getenv("LOCALAPPDATA") or ""
        if local:
            base = Path(local) / "ms-playwright"
            if base.is_dir():
                for exe in sorted(base.glob("chromium-*/chrome-win*/chrome.exe")):
                    if exe.exists():
                        return str(exe)
    except Exception:
        pass
    return ""


def free_port() -> int:
    """要一个空闲端口。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ══════════════════════════════════════════════════════════
# CDP 小客户端
# ══════════════════════════════════════════════════════════

class Cdp:
    """极简 CDP 客户端（只用标准库 + websockets，不引入新依赖）。"""

    def __init__(self, ws_url: str, timeout: float = 10.0):
        import websockets.sync.client as wsc  # 延迟导入：缺依赖时才报
        self._ws = wsc.connect(ws_url, max_size=None, open_timeout=timeout)
        self._id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        mid = self._id
        self._ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self._ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method} 失败: {msg['error']}")
                return msg.get("result", {})

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


def _http_json(url: str, timeout: float = 2.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def wait_for_cdp(port: int, timeout: float) -> dict | None:
    """等 Chrome 调试端口可用。超时返回 None，不抛异常。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            return _http_json(f"http://127.0.0.1:{port}/json/version")
        except Exception:
            time.sleep(0.4)
    return None


def pick_page(port: int) -> dict | None:
    """挑一个页面作为 CDP 入口（优先 x.com）。"""
    try:
        targets = _http_json(f"http://127.0.0.1:{port}/json")
    except Exception:
        return None
    pages = [t for t in targets if t.get("type") == "page"]
    for t in pages:
        if "x.com" in (t.get("url") or ""):
            return t
    return pages[0] if pages else None


# ══════════════════════════════════════════════════════════
# 登录态读取与导出
# ══════════════════════════════════════════════════════════

def read_cookies(port: int) -> list[dict]:
    """通过 CDP 读取浏览器当前全部 cookie（"问 Chrome 本人要"）。"""
    page = pick_page(port)
    if not page:
        return []
    cdp = Cdp(page["webSocketDebuggerUrl"])
    try:
        cdp.call("Network.enable")
        return cdp.call("Network.getAllCookies").get("cookies", []) or []
    finally:
        cdp.close()


def has_auth(cookies: list[dict]) -> bool:
    """是否已登录（auth_token 非空）。"""
    return any(c.get("name") == AUTH_COOKIE and c.get("value") for c in cookies or [])


def read_local_storage(port: int) -> dict[str, str]:
    """取 x.com 的 localStorage（storage_state 需要它才算完整）。"""
    page = pick_page(port)
    if not page:
        return {}
    cdp = Cdp(page["webSocketDebuggerUrl"])
    try:
        cdp.call("DOMStorage.enable")
        out: dict[str, str] = {}
        for origin in ("https://x.com", "https://twitter.com"):
            try:
                items = cdp.call("DOMStorage.getDOMStorageItems",
                                 {"storageId": {"securityOrigin": origin,
                                                "isLocalStorage": True}})
            except Exception:
                continue
            for k, v in items.get("entries", []) or []:
                out[str(k)] = str(v)
        return out
    finally:
        cdp.close()


def _read_account_from_browser(port: int) -> str:
    """从浏览器里读出当前登录的账号，并记进 settings。失败返回空串。

    用 CDP 在页面里取左下角账号切换按钮的文本（形如
    "显示名 | @handle"）—— 这是最可靠的来源。
    """
    page = pick_page(port)
    if not page:
        return ""
    cdp = Cdp(page["webSocketDebuggerUrl"])
    try:
        expr = (
            "(function(){"
            " var s='[data-testid=\"SideNav_AccountSwitcher_Button\"]';"
            " var el=document.querySelector(s);"
            " if(!el) return '';"
            " return (el.innerText||'').trim();"
            "})()"
        )
        res = cdp.call("Runtime.evaluate",
                       {"expression": expr, "returnByValue": True})
        text = str(((res.get("result") or {}).get("value")) or "")
        handle = ""
        for part in text.replace("\n", "|").split("|"):
            q = part.strip()
            if q.startswith("@") and 1 < len(q) <= 21:
                handle = q
                break
        display = ""
        for part in text.replace("\n", "|").split("|"):
            q = part.strip()
            if q and not q.startswith("@") and len(q) < 60:
                display = q
                break
        if handle or display:
            try:
                from core import settings as _settings
                val = f"{display} {handle}".strip()
                _settings.set_many({"x_username": val[:200]})
            except Exception:
                pass
            return f"{display} {handle}".strip()
        return ""
    finally:
        cdp.close()


def to_storage_state(port: int) -> dict:
    """转成 Playwright `storage_state.json` 的结构（cookies + origins）。

    ⚠ 只保留 x.com / twitter.com 的 cookie。别站 cookie 绝不能带出去。
    """
    raw = read_cookies(port)
    cookies: list[dict] = []
    for c in raw or []:
        dom = c.get("domain", "") or ""
        name = c.get("name", "") or ""
        # CDP 偶发返回缺 domain 的条目。X 的认证 cookie 必须保住，
        # 否则会静默丢掉登录态（"导出了文件但用不了"）。
        is_auth = name in AUTH_COOKIES
        if not is_auth and not ("x.com" in dom or "twitter.com" in dom):
            continue
        if is_auth and not dom:
            dom = ".x.com"
        cookies.append({
            "name": name,
            "value": c.get("value", "") or "",
            "domain": dom,
            "path": c.get("path", "/") or "/",
            "expires": c.get("expires", -1),
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", False)),
            "sameSite": {"Strict": "Strict", "Lax": "Lax",
                         "None": "None"}.get(str(c.get("sameSite", "")), "Lax"),
        })

    ls = read_local_storage(port)
    origins = [{"origin": "https://x.com",
                "localStorage": [{"name": k, "value": v} for k, v in ls.items()]}] if ls else []
    return {"cookies": cookies, "origins": origins}


# ══════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════

def login_via_chrome(*, state_path, profile_dir=None, timeout: int = 600,
                     on_event=None, chrome_path: str = "") -> tuple[bool, str]:
    """启动真 Chrome，等用户登录，导出登录态到 `state_path`。

    **绝不抛异常**。返回 (是否成功, 可读中文说明)。
    `on_event(str)` 可选：进度回调。
    """
    def emit(msg: str) -> None:
        if on_event:
            try:
                on_event(str(msg))
            except Exception:
                pass

    state = Path(state_path)
    prof = Path(profile_dir) if profile_dir else state.parent / "chrome-profile"
    chrome = (chrome_path or "").strip() or find_chrome()

    if not chrome:
        return False, ("找不到 Chrome。请安装 Google Chrome，"
                       "或用环境变量 CHROME_PATH 指定 chrome.exe 的完整路径。")

    # 无图形界面时提前拒绝，避免用户看到一堆 Chrome 底层报错（或卡死）
    if not has_display():
        return False, GUI_HINT

    proc = None
    try:
        prof.mkdir(parents=True, exist_ok=True)
        port = free_port()
        emit(f"正在启动 Chrome（{Path(chrome).name}），请在弹出的窗口里登录 X……")
        cmd = [chrome, f"--remote-debugging-port={port}",
               f"--user-data-dir={prof}",
               "--no-first-run", "--no-default-browser-check",
               "--new-window", X_LOGIN_URL]
        # 容器/root 下 Chrome 需要关闭沙箱才能启动
        try:
            if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
                cmd.insert(1, "--no-sandbox")
                cmd.insert(2, "--disable-dev-shm-usage")
        except Exception:
            pass
        creation = 0
        if os.name == "nt":
            creation = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, creationflags=creation)
        except Exception as e:
            return False, f"启动 Chrome 失败：{type(e).__name__}: {e}"

        ver = wait_for_cdp(port, timeout=CDP_WAIT_SECONDS)
        if not ver:
            return False, ("Chrome 的调试端口没起来（可能被安全软件拦了）。"
                           "可尝试关闭所有 Chrome 窗口后重试。")
        emit(f"Chrome 已就绪（{ver.get('Browser', '?')}），等待登录……")

        deadline = time.time() + max(int(timeout), 30)
        last_note = 0.0
        while time.time() < deadline:
            try:
                if has_auth(read_cookies(port)):
                    break
            except Exception:
                pass
            now = time.time()
            if now - last_note >= 15:
                emit(f"仍在等待登录……（剩余 {int(deadline - now)} 秒）")
                last_note = now
            time.sleep(POLL_SECONDS)
        else:
            return False, f"登录超时（{timeout} 秒内未检测到登录成功），请重试。"

        emit("检测到登录成功（auth_token 已就位），正在导出登录态……")
        time.sleep(2.0)          # 等 x.com 写全 cookie
        try:
            st = to_storage_state(port)
        except Exception as e:
            return False, f"导出登录态失败：{type(e).__name__}: {e}"
        if not st.get("cookies"):
            return False, "没读到任何 x.com cookie，导出结果为空。"

        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(state, 0o600)      # 登录态等同账号凭据
        except Exception:
            pass
        # 顺手把登录的账号名记下来 —— 否则用户只知道"保存了登录态"，
        # 不知道登的是哪个号（多账号场景尤其要命）。
        try:
            who = _read_account_from_browser(port)
            if who:
                emit(f"识别到账号：{who}")
        except Exception as e:
            log.debug("读取账号名失败(忽略): %s", e)

        emit(f"登录态已保存（{len(st['cookies'])} 个 cookie）")
        return True, f"登录成功，已保存凭据（{state}）"
    except Exception as e:  # 兜底：对外绝不抛异常
        return False, f"登录过程异常：{type(e).__name__}: {e}"
    finally:
        if proc is not None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except Exception:
                    proc.kill()
            except Exception:
                pass
