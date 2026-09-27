"""控制台多账号与账号级数据隔离。

设计目标：
  * 进入控制台必须先输入账号密码；
  * 每个账号拥有完全独立的数据目录、SQLite 队列、设置、媒体与浏览器登录态；
  * 不修改冻结的 core.config / core.queue / core.settings，只通过运行期路径路由实现隔离。

实现方式：
  * ``use_account()`` 用 ContextVar 标记当前账号；
  * ``install_path_router()`` 把 config 中的路径替换成账号感知的代理对象；
  * queue/settings/backends 每次访问 config.DB_PATH / MEDIA_DIR / BROWSER_DIR 时，
    代理都会按当前 ContextVar 解析到对应账号目录。

安全约定：浏览器登录控制台的密码不落库、不写日志；会话 Cookie 只携带用户名和
HMAC 签名，不携带密码。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any, Iterator

from . import config

log = logging.getLogger("twitbot.accounts")

ACCOUNT_NAMES: tuple[str, ...] = ("qwqcon", "luoaowoo", "bot")
COOKIE_NAME = "twitbot_session"
SESSION_DAYS = 30

# 每个控制台账号固定绑定自己的登录密码。
_ACCOUNT_PASSWORDS: dict[str, str] = {
    "qwqcon": "qwqcon_qwqcon",
    "luoaowoo": "luoaowoo_luoaowoo",
    "bot": "luoaowoo",
}

_current_account: ContextVar[str | None] = ContextVar(
    "twitbot_current_account", default=None)
_base_data_dir: Path | None = None
_router_base: Path | None = None
_install_lock = threading.Lock()
_installed = False
_session_secret: bytes | None = None
_shared_state: dict[str, Any] = {}


def shared_state(key: str, factory: Any) -> Any:
    """进程级共享状态；模块被测试重载时仍保持同一对象。"""
    if key not in _shared_state:
        _shared_state[key] = factory()
    return _shared_state[key]


def auth_enabled() -> bool:
    """控制台登录是否启用。

    测试通过 ``TWITBOT_AUTH_DISABLED=1`` 关闭，以免大量旧接口测试被登录门禁挡住；
    生产默认始终开启。
    """
    return (os.getenv("TWITBOT_AUTH_DISABLED", "").strip().lower()
            not in ("1", "true", "yes", "on"))


def normalize_username(username: str | None) -> str:
    return (username or "").strip().lower()


def is_known_account(username: str | None) -> bool:
    return normalize_username(username) in ACCOUNT_NAMES


def account_names() -> tuple[str, ...]:
    return ACCOUNT_NAMES


def current_account() -> str | None:
    """当前上下文中的账号；公开页面/旧接口可能为 None。"""
    name = _current_account.get()
    return name if is_known_account(name) else None


@contextmanager
def use_account(username: str | None) -> Iterator[None]:
    """在上下文中切换账号；退出时恢复，线程/异步任务之间互不串联。"""
    norm = normalize_username(username)
    token: Token = _current_account.set(norm if is_known_account(norm) else None)
    try:
        yield
    finally:
        _current_account.reset(token)


def _base_dir() -> Path:
    global _base_data_dir
    if _base_data_dir is None:
        raw = config.DATA_DIR
        if isinstance(raw, _AccountPath):
            if _router_base is None:
                raise RuntimeError("账号路径路由缺少基础数据目录")
            _base_data_dir = _router_base
        else:
            _base_data_dir = Path(raw).resolve()
    return _base_data_dir


def account_dir(username: str | None = None) -> Path:
    """账号根目录；username=None 时取当前上下文账号，无账号则返回旧版根目录。"""
    name = current_account() if username is None else normalize_username(username)
    if not is_known_account(name):
        return _base_dir()
    return _base_dir() / "accounts" / name


def _account_root_for_context() -> Path:
    return account_dir(current_account())


class _AccountPath:
    """按当前账号解析的 PathLike 代理。

    冻结模块里既有 ``config.DATA_DIR / "x"``，也有 ``Path(config.MEDIA_DIR)``；
    因此这个代理同时实现 ``__fspath__``、``__truediv__`` 和常用 Path 属性转发。
    """

    __slots__ = ("_kind",)

    def __init__(self, kind: str) -> None:
        self._kind = kind

    def _real(self) -> Path:
        root = _account_root_for_context()
        if self._kind == "data":
            return root
        if self._kind == "db":
            return root / "queue.db"
        if self._kind == "media":
            return root / "media"
        if self._kind == "browser":
            return root / "browser"
        if self._kind == "logs":
            return root / "logs"
        raise KeyError(self._kind)

    def __fspath__(self) -> str:
        return str(self._real())

    def __str__(self) -> str:
        return str(self._real())

    def __repr__(self) -> str:
        return f"AccountPath({self._kind!r}) -> {self._real()}"

    def __truediv__(self, other: Any) -> Path:
        return self._real() / other

    def __rtruediv__(self, other: Any) -> Path:
        return Path(other) / self._real()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real(), name)


def install_path_router() -> None:
    """安装账号感知路径代理（幂等）。必须在启动器/应用创建早期调用。"""
    global _installed, _base_data_dir, _router_base
    with _install_lock:
        if _installed:
            raw = config.DATA_DIR
            if not isinstance(raw, _AccountPath):
                # 测试/嵌入方可能显式换了 DATA_DIR；同步更新基础目录。
                _router_base = Path(raw).resolve()
                _base_data_dir = _router_base
            return
        # 先把原始路径固定下来，之后 _base_dir 不再递归读取代理。
        _router_base = Path(config.DATA_DIR).resolve()
        _base_data_dir = _router_base
        config.DATA_DIR = _AccountPath("data")       # type: ignore[assignment]
        config.DB_PATH = _AccountPath("db")          # type: ignore[assignment]
        config.MEDIA_DIR = _AccountPath("media")     # type: ignore[assignment]
        config.BROWSER_DIR = _AccountPath("browser") # type: ignore[assignment]
        config.LOG_DIR = _AccountPath("logs")        # type: ignore[assignment]
        _installed = True


def ensure_account_dirs(username: str) -> Path:
    """创建单账号的完整目录结构并返回账号根目录。"""
    name = normalize_username(username)
    if not is_known_account(name):
        raise ValueError(f"未知账号：{username!r}")
    root = account_dir(name)
    for p in (root, root / "media", root / "browser", root / "logs",
              root / "memes", root / "collect"):
        p.mkdir(parents=True, exist_ok=True)
    return root


def init_accounts() -> list[Path]:
    """初始化所有账号目录、队列表和 settings 表。"""
    install_path_router()
    roots: list[Path] = []
    for name in ACCOUNT_NAMES:
        root = ensure_account_dirs(name)
        with use_account(name):
            from . import queue, settings
            queue.init_db()
            settings.all_settings()
        roots.append(root)
    return roots


def _password_candidates(username: str) -> tuple[str, ...]:
    name = normalize_username(username)
    password = _ACCOUNT_PASSWORDS.get(name, "")
    return (password,) if password else ()


def verify_password(username: str, password: str) -> bool:
    """校验控制台账号密码，使用常量时间比较。"""
    name = normalize_username(username)
    if not is_known_account(name) or password is None:
        return False
    given = str(password).encode("utf-8")
    return any(hmac.compare_digest(given, candidate.encode("utf-8"))
               for candidate in _password_candidates(name))


def _secret_path() -> Path:
    return _base_dir() / ".session_secret"


def _load_session_secret() -> bytes:
    global _session_secret
    if _session_secret is not None:
        return _session_secret
    p = _secret_path()
    try:
        if p.is_file():
            raw = p.read_bytes().strip()
            if len(raw) >= 32:
                _session_secret = raw
                return raw
    except Exception:
        pass
    raw = secrets.token_bytes(48)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(raw)
        try:
            os.chmod(p, 0o600)
        except Exception:
            pass
    except Exception as e:
        log.warning("会话密钥文件写入失败，本次运行使用临时密钥：%s", e)
    _session_secret = raw
    return raw


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode((text + pad).encode("ascii"))


def issue_session(username: str, ttl_seconds: int = SESSION_DAYS * 86400) -> str:
    name = normalize_username(username)
    if not is_known_account(name):
        raise ValueError(f"未知账号：{username!r}")
    expires = int(time.time()) + max(60, int(ttl_seconds))
    nonce = secrets.token_urlsafe(12)
    body = f"{name}|{expires}|{nonce}".encode("utf-8")
    sig = hmac.new(_load_session_secret(), body, hashlib.sha256).digest()
    return f"{_b64e(body)}.{_b64e(sig)}"


def verify_session(token: str | None) -> str | None:
    """验证 Cookie 会话；有效返回用户名，否则返回 None。"""
    if not token or "." not in token:
        return None
    try:
        body_b64, sig_b64 = token.split(".", 1)
        body = _b64d(body_b64)
        sig = _b64d(sig_b64)
        expected = hmac.new(_load_session_secret(), body, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        name, expires_s, _nonce = body.decode("utf-8").split("|", 2)
        name = normalize_username(name)
        if not is_known_account(name) or int(expires_s) < int(time.time()):
            return None
        return name
    except Exception:
        return None


__all__ = [
    "ACCOUNT_NAMES", "COOKIE_NAME", "account_dir", "account_names",
    "auth_enabled", "current_account", "ensure_account_dirs", "init_accounts",
    "install_path_router", "is_known_account", "issue_session",
    "normalize_username", "shared_state", "use_account", "verify_password",
    "verify_session",
]
