#!/usr/bin/env python3
"""Telegram 机器人运行时管理器（★E）—— 供 Web 控制台 / 统一启动器启停机器人。

职责：把「起/停长轮询、配 token、看状态」封装成一组**永不抛异常**的方法，
让 Web 控制台（`web/server.py` 的 `/api/tg/*`）与统一启动器（`start.py`）都能直接调。

设计约束（见 CONTRACT_V2.md §2.2）：

* **绝不抛异常**：所有对外方法内部 try/except，失败返回 `(False, 中文原因)`。
  唯一例外是把 `asyncio.CancelledError` / `KeyboardInterrupt` 原样上抛 ——
  吞掉它们会让 Ctrl+C 与请求取消失效。
* **import 期零副作用**：本模块顶部**不 import** `telegram` / `bot` / `core.config`，
  全部延迟到真正调用时（`_load_bot_module()` / `_settings()` / `_config()`）。
  因此 `import core.tgmanager` 不联网、不连 Telegram、不建目录。
* **token 绝不外泄**：`status()` 只给 `token_masked` + `token_set`；所有对外文案
  （含异常说明、`last_error`）都过 `_scrub()` 二次脱敏 —— 因为 PTB 的
  `InvalidToken` 消息里**原样带着 token**（"The token ... was rejected"）。
* **幂等**：重复 `start()` 返回 `(True, "机器人已在运行")`，不重复起轮询。
* 复用 `bot.build_application()` 构造 Application，不自己重写 handler 注册。

与 `start.py` 的关系：`start.py` 会 `mgr = TelegramManager(); await mgr.start()`，
退出时 `await mgr.stop()` 干净收尾。

**注意（集成方必读）**：本管理器走 `initialize()/start()/updater.start_polling()`，
**不会**触发 `bot.post_init` / `bot.post_shutdown`（那只有 `run_polling` 才调）。
所以发布循环必须由调用方自己跑 —— `start.py` 已经是自己起 `Pipeline` 的写法；
如果控制台期望"起机器人顺带起发布循环"，请显式另起 Pipeline，不要靠本模块。
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

__all__ = ["TelegramManager", "TgDependencyError", "mask_token"]

log = logging.getLogger("twitbot.tgmanager")


# ══════════════════════════════════════════════════════════
# 错误类型与文案
# ══════════════════════════════════════════════════════════

class TgDependencyError(RuntimeError):
    """python-telegram-bot / bot.py 不可用（缺依赖等），调用方据此给中文提示。"""


MSG_NO_TOKEN = "缺少 Telegram bot token：请在控制台填写，或在 .env 里配置 TG_TOKEN"
MSG_NO_DEPS = ("缺少 python-telegram-bot 依赖：请在项目目录执行 "
               ".venv\\Scripts\\python.exe -m pip install -r requirements.txt")
MSG_BAD_TOKEN = "token 无效或已失效，请到 @BotFather 重新获取"
MSG_TOKEN_FORMAT = "token 格式不正确（应形如 123456789:AA...），请到 @BotFather 重新获取"
MSG_NETWORK = "网络不可达：无法连接 Telegram（请检查代理/VPN 或网络）"
MSG_CONFLICT = ("该 token 已在别处轮询：同一 token 同时只能有一个长轮询实例，"
                "请先停掉其它正在运行的实例")
MSG_ALREADY = "机器人已在运行"
MSG_STARTING = "机器人正在启动中，请稍候"
MSG_NOT_RUNNING = "机器人未在运行"
MSG_STOPPED = "机器人已停止"

# 按异常类名映射成人话（不做 telegram import，测试可离线跑）。
_ERR_BY_NAME = {
    "InvalidToken": MSG_BAD_TOKEN,
    "Forbidden": "机器人被对方拉黑或无权限（请检查是否已与机器人建立会话）",
    "TimedOut": "网络不可达：连接 Telegram 超时（请检查代理/VPN 或网络）",
    "NetworkError": MSG_NETWORK,
    "EndPointNotFound": "Telegram API 地址不可达（可能被网络环境拦截，请检查代理/VPN）",
    "BadRequest": "Telegram 拒绝了请求（请检查 token 与网络环境）",
    "Conflict": MSG_CONFLICT,
}

# 类名没命中时的文案线索（PTB 会把同一类错误包进不同子类）。
_ERR_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("rejected by the server", "you must pass the token", "unauthorized",
      "invalid token", "token was rejected"), MSG_BAD_TOKEN),
    (("terminated by other getupdates",), MSG_CONFLICT),
    (("timed out", "connect timeout", "cannot connect", "connection error"), MSG_NETWORK),
)

# token 形状兜底：脱敏用（覆盖 PTB 在异常消息里回显 token 的情形）
_TOKEN_BLOB_RE = re.compile(r"\d{5,}:[A-Za-z0-9_\-]{15,}")

# 延迟导入缓存（只缓存 telegram 的 bot 模块）
_BOT_MODULE: Any = None


def _load_bot_module() -> Any:
    """延迟导入项目根的 `bot.py`（**复用**它的 build_application，不重写 handler）。

    导入失败分两类给出人话：缺 python-telegram-bot 依赖 / bot.py 自身有问题。
    """
    global _BOT_MODULE
    if _BOT_MODULE is not None:
        return _BOT_MODULE
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        import bot as botmod  # noqa: PLC0415
    except ImportError as e:
        missing = str(getattr(e, "name", "") or "")
        if missing.startswith("telegram") or "telegram" in missing:
            raise TgDependencyError(MSG_NO_DEPS) from e
        raise TgDependencyError(f"bot.py 导入失败：{type(e).__name__}: {e}") from e
    except Exception as e:
        raise TgDependencyError(f"bot.py 导入失败：{type(e).__name__}: {e}") from e
    _BOT_MODULE = botmod
    return botmod


def _reset_bot_module_cache() -> None:
    """测试用：清掉 bot 模块缓存。"""
    global _BOT_MODULE
    _BOT_MODULE = None


def _settings():
    from core import settings  # noqa: PLC0415
    return settings


def _config():
    from core import config  # noqa: PLC0415
    return config


def mask_token(token: str) -> str:
    """把 token 脱敏成可安全展示的形式（如 ``1234...:AAH***xyz``）。空 token → ""。

    约定：返回值**永不包含完整 token**，也**永不包含冒号后的密钥明文**
    （密钥段只有长度足够时才保留首尾 3 字符，给用户"对号"用）。
    """
    t = (token or "").strip()
    if not t:
        return ""
    if ":" in t:
        head, _, secret = t.partition(":")
        head_part = f"{head[:4]}..." if len(head) > 4 else f"{head[:4]}..." if head else "***"
        secret_part = f"{secret[:3]}***{secret[-3:]}" if len(secret) > 8 else "***"
        return f"{head_part}:{secret_part}"
    if len(t) > 10:
        return f"{t[:4]}***{t[-3:]}"
    return "***"


# ══════════════════════════════════════════════════════════
# 管理器
# ══════════════════════════════════════════════════════════

_EMPTY_STATUS: dict[str, Any] = {
    "running": False,
    "token_set": False,
    "token_masked": "",
    "token_source": "",
    "allowed_users": "",
    "allowed_chats": "",
    "bot_username": "",
    "bot_id": 0,
    "started_at": 0.0,
    "last_error": "",
    "last_update_at": 0.0,
    "updates": 0,
}


class TelegramManager:
    """Telegram 长轮询的运行时管理器（单实例对象，供控制台与启动器共用）。

    线程/协程安全性：`start()` 用同步标志位 `_starting` 挡住并发双起
    （不使用 `asyncio.Lock` —— 那会把对象绑死到某一个事件循环，跨 `asyncio.run`
    复用就会报 `got Future attached to a different loop`）。
    """

    on_log: Callable[[str], None] | None = None

    def __init__(self, on_log: Callable[[str], None] | None = None) -> None:
        self.on_log = on_log
        self._app: Any = None
        self._botmod: Any = None
        self._running = False
        self._starting = False
        self._started_at = 0.0
        self._last_error = ""
        self._last_update_at = 0.0
        self._updates = 0
        self._bot_username = ""
        self._bot_id = 0
        self._token = ""            # 私有：仅用于异常文案二次脱敏，绝不对外返回

    # ── 内部小工具 ────────────────────────────────────────

    def _log(self, msg: str) -> None:
        """进度回调 + 日志。回调自身出错绝不影响主流程。"""
        try:
            log.info("%s", msg)
        except Exception:
            pass
        cb = self.on_log
        if cb is not None:
            try:
                cb(str(msg))
            except Exception as e:  # 回调是外部代码，必须兜住
                try:
                    log.warning("on_log 回调异常（忽略）：%s", type(e).__name__)
                except Exception:
                    pass

    def _scrub(self, text: Any, extra_secret: str = "") -> str:
        """把文本里可能出现的 token 换掉 —— 对外文案的最后一道防线。"""
        s = str(text or "")
        for secret in (self._token, extra_secret):
            s2 = (secret or "").strip()
            if len(s2) >= 8 and s2 in s:
                s = s.replace(s2, mask_token(s2))
        try:
            s = _TOKEN_BLOB_RE.sub(lambda m: mask_token(m.group(0)), s)
        except Exception:
            pass
        return s

    def _friendly_error(self, exc: BaseException, extra_secret: str = "") -> str:
        """异常 → 中文人话（按类名映射；未识别的走脱敏后的原文）。"""
        name = type(exc).__name__
        if isinstance(exc, SystemExit):
            raw = getattr(exc, "code", None)
            return self._scrub(raw if isinstance(raw, str) else (str(exc) or MSG_NO_TOKEN),
                               extra_secret)
        if name == "RetryAfter":
            try:
                secs = int(getattr(exc, "retry_after", 0) or 0)
            except Exception:
                secs = 0
            return (f"Telegram 限流：请等待 {secs} 秒后重试" if secs
                    else "Telegram 限流：请稍后重试")
        for cls in type(exc).__mro__:
            msg = _ERR_BY_NAME.get(cls.__name__)
            if msg:
                return msg
        # 类名没命中（PTB 子类 / 假异常）：按消息线索再判一次，仍无则给脱敏原文
        low = str(exc).lower()
        for needles, hint in _ERR_HINTS:
            if any(n in low for n in needles):
                return hint
        return self._scrub(f"操作失败：{name}: {exc}", extra_secret)

    @staticmethod
    def _me_info(app: Any) -> dict:
        """从已 initialize 的 Application 取 bot 用户名/id（取不到给空值）。"""
        info: dict[str, Any] = {"bot_username": "", "bot_id": 0}
        try:
            b = getattr(app, "bot", None)
            if b is None:
                return info
            try:
                info["bot_username"] = str(getattr(b, "username", "") or "")
            except Exception:
                info["bot_username"] = ""
            try:
                info["bot_id"] = int(getattr(b, "id", 0) or 0)
            except Exception:
                info["bot_id"] = 0
        except Exception:
            pass
        return info

    def _resolve_token(self) -> tuple[str, str]:
        """token 取用顺序：settings.tg_token（控制台可配）→ config.TG_TOKEN（.env）。"""
        try:
            v = (_settings().get("tg_token", "") or "").strip()
            if v:
                return v, "settings"
        except Exception as e:
            log.debug("读取 settings.tg_token 失败（忽略）：%s", type(e).__name__)
        try:
            v = str(getattr(_config(), "TG_TOKEN", "") or "").strip()
            if v:
                return v, "env"
        except Exception as e:
            log.debug("读取 config.TG_TOKEN 失败（忽略）：%s", type(e).__name__)
        return "", ""

    def _effective_allowed(self, key: str, config_attr: str) -> str:
        """白名单展示值：settings 非空优先，空则显示 config 里的实际生效值。"""
        try:
            raw = (_settings().get(key, "") or "").strip()
            if raw:
                return raw
        except Exception:
            pass
        try:
            vals = getattr(_config(), config_attr, set()) or set()
            return ",".join(str(x) for x in sorted(vals))
        except Exception:
            return ""

    def _sync_bot_stats(self) -> None:
        bm = self._botmod
        if bm is None:
            return
        try:
            fn = getattr(bm, "updates_seen", None)
            if callable(fn):
                self._updates = int(fn() or 0)
        except Exception:
            pass
        try:
            fn = getattr(bm, "last_update_at", None)
            if callable(fn):
                self._last_update_at = float(fn() or 0.0)
        except Exception:
            pass

    def _refresh_running(self) -> None:
        """以 updater.running 为准，只做 True→False 的降级（轮询自己死了要能看出来）。"""
        app = self._app
        if app is None:
            if self._running:
                self._running = False
                self._started_at = 0.0
            return
        try:
            up = getattr(app, "updater", None)
            if up is not None and hasattr(up, "running") and self._running:
                if not bool(getattr(up, "running")):
                    log.warning("检测到长轮询已停止，状态标记为未运行")
                    self._running = False
                    self._started_at = 0.0
        except Exception:
            pass
        self._sync_bot_stats()

    async def _teardown_app(self, app: Any) -> list[str]:
        """尽最大努力逐层收尾，返回警告列表（每层单独兜底，互不拖累）。"""
        warns: list[str] = []
        if app is None:
            return warns
        updater = getattr(app, "updater", None)
        if updater is not None:
            try:
                if bool(getattr(updater, "running", False)):
                    await updater.stop()
            except Exception as e:
                warns.append(self._scrub(f"updater.stop {type(e).__name__}: {e}"))
        try:
            if bool(getattr(app, "running", False)):
                await app.stop()
        except Exception as e:
            warns.append(self._scrub(f"app.stop {type(e).__name__}: {e}"))
        try:
            await app.shutdown()
        except Exception as e:
            warns.append(self._scrub(f"app.shutdown {type(e).__name__}: {e}"))
        for w in warns:
            log.warning("Telegram 收尾告警：%s", w)
        return warns

    def _mark_failed(self, reason: str) -> None:
        self._last_error = self._scrub(reason)

    # ── 查询（同步、快、不联网）────────────────────────────

    def status(self) -> dict:
        """机器人与 token 状态快照。**永不抛异常**，且**绝不返回完整 token**。"""
        out = dict(_EMPTY_STATUS)
        try:
            self._refresh_running()
            tok, src = self._resolve_token()
            out.update(
                running=bool(self._running),
                token_set=bool(tok),
                token_masked=mask_token(tok),
                token_source=src,
                allowed_users=self._effective_allowed("tg_allowed_users", "ALLOWED_USERS"),
                allowed_chats=self._effective_allowed("tg_allowed_chats", "ALLOWED_CHATS"),
                bot_username=str(self._bot_username or ""),
                bot_id=int(self._bot_id or 0),
                started_at=float(self._started_at or 0.0),
                last_error=self._scrub(self._last_error or "", tok),
                last_update_at=float(self._last_update_at or 0.0),
                updates=int(self._updates or 0),
            )
        except Exception as e:  # 最后兜底：字段必须齐全，控制台不能 500
            log.warning("status() 兜底：%s: %s", type(e).__name__, self._scrub(str(e)))
            out["last_error"] = out["last_error"] or f"状态读取异常：{type(e).__name__}"
        return out

    # ── 校验（异步，只校验不启动）──────────────────────────

    async def verify_token(self, token: str) -> tuple[bool, str, dict]:
        """只校验 token 有效性与拿 get_me，**不启动轮询**。不抛异常。"""
        info: dict[str, Any] = {"bot_username": "", "bot_id": 0}
        tok = (token or "").strip()
        if not tok:
            return False, "请先填写 bot token（形如 123456789:AA...）", info
        if ":" not in tok or len(tok) < 20:
            return False, MSG_TOKEN_FORMAT, info
        try:
            botmod = _load_bot_module()
        except TgDependencyError as e:
            return False, self._scrub(str(e), tok), info
        except BaseException as e:
            if isinstance(e, (asyncio.CancelledError, KeyboardInterrupt)):
                raise
            return False, self._friendly_error(e, tok), info

        app = None
        try:
            app = botmod.build_application(tok)
            await app.initialize()                 # 内部调 getMe；token 错会在这里抛
            info = self._me_info(app)
            name = info.get("bot_username") or ""
            return True, (f"token 有效：@{name}" if name else "token 有效"), info
        except BaseException as e:
            if isinstance(e, (asyncio.CancelledError, KeyboardInterrupt)):
                raise
            return False, self._friendly_error(e, tok), info
        finally:
            if app is not None:
                await self._teardown_app(app)

    # ── 控制（异步）────────────────────────────────────────

    async def start(self, token: str | None = None,
                    allowed_users: str | None = None,
                    allowed_chats: str | None = None) -> tuple[bool, str]:
        """启动长轮询（幂等）。成功顺手 get_me 拿 bot_username/bot_id。不抛异常。

        * `token=None` → settings.tg_token → config.TG_TOKEN 顺序取。
        * 已经在跑 → `(True, "机器人已在运行")`；此时**仍会**应用传入的新白名单
          （白名单是每轮 update 现读的运行期值，无需重启即生效）。
        * `allowed_*`：None = 不动；"" = 清空（回落 config）；非空 = 落库。
        """
        self._refresh_running()
        if self._running:
            self._persist_allowed(allowed_users, allowed_chats)
            return True, MSG_ALREADY
        if self._starting:
            return False, MSG_STARTING
        self._starting = True

        app: Any = None
        tok = ""
        explicit = (token or "").strip()
        try:
            # 上一轮可能异常退出但没清干净：先收尾，避免旧 Application 泄漏
            if self._app is not None:
                await self._teardown_app(self._app)
                self._app = None

            tok = explicit
            if not tok:
                tok, _ = self._resolve_token()
            if not tok:
                self._mark_failed(MSG_NO_TOKEN)
                return False, MSG_NO_TOKEN

            try:
                botmod = _load_bot_module()
            except TgDependencyError as e:
                self._mark_failed(str(e))
                return False, self._scrub(str(e), tok)

            self._log("正在构造 Telegram Application…")
            app = botmod.build_application(tok)     # 复用 bot.py 的唯一注册入口

            self._log("正在校验 token（getMe）…")
            await app.initialize()

            info = self._me_info(app)
            self._log("正在启动 Application…")
            await app.start()

            updater = getattr(app, "updater", None)
            if updater is None:
                raise RuntimeError("Application 没有 updater，无法启动长轮询")
            self._log("正在启动长轮询…")
            await updater.start_polling(drop_pending_updates=False)

            # 全部成功后才认账
            self._app = app
            self._botmod = botmod
            self._token = tok
            self._running = True
            self._started_at = time.time()
            self._last_error = ""
            self._updates = 0
            self._last_update_at = 0.0
            self._bot_username = str(info.get("bot_username") or "")
            self._bot_id = int(info.get("bot_id") or 0)
            self._reset_bot_stats()

            # 全部成功后才落库 —— 无效 token 不该被写进 settings（控制台别存垃圾）
            if explicit:
                self._persist_token(explicit)
            self._persist_allowed(allowed_users, allowed_chats)

            name = self._bot_username
            msg = f"机器人已启动：@{name}" if name else "机器人已启动"
            self._log(msg)
            return True, msg
        except BaseException as e:
            if isinstance(e, (asyncio.CancelledError, KeyboardInterrupt)):
                await self._teardown_app(app)
                raise
            if app is not None:
                await self._teardown_app(app)
            self._app = None
            self._running = False
            self._started_at = 0.0
            reason = self._friendly_error(e, tok or explicit)
            if isinstance(e, TgDependencyError):
                reason = self._scrub(str(e), tok)
            self._mark_failed(reason)
            log.warning("Telegram 启动失败：%s", reason)
            return False, reason
        finally:
            self._starting = False

    async def stop(self) -> tuple[bool, str]:
        """停止轮询并释放。未运行 → (True, "机器人未在运行")。不抛异常。"""
        try:
            app = self._app
            self._refresh_running()
            if app is None and not self._running:
                self._app = None
                self._starting = False
                self._started_at = 0.0
                return True, MSG_NOT_RUNNING

            self._log("正在停止 Telegram 机器人…")
            warns = await self._teardown_app(app)
            self._app = None
            self._running = False
            self._starting = False
            self._started_at = 0.0
            if warns:
                msg = f"{MSG_STOPPED}（收尾告警：{'；'.join(warns)}）"
            else:
                msg = MSG_STOPPED
            self._log(msg)
            return True, msg
        except BaseException as e:
            if isinstance(e, (asyncio.CancelledError, KeyboardInterrupt)):
                raise
            self._app = None
            self._running = False
            self._started_at = 0.0
            reason = self._scrub(f"停止失败：{type(e).__name__}: {e}")
            self._mark_failed(reason)
            return False, reason

    async def restart(self, **kw) -> tuple[bool, str]:
        """stop() 再 start()。不抛异常。"""
        try:
            ok_stop, msg_stop = await self.stop()
            if not ok_stop:
                return False, f"重启失败（停止阶段）：{msg_stop}"
            ok_start, msg_start = await self.start(**kw)
            if not ok_start:
                return False, f"已停止，但重新启动失败：{msg_start}"
            return True, f"已重启：{msg_start}"
        except BaseException as e:
            if isinstance(e, (asyncio.CancelledError, KeyboardInterrupt)):
                raise
            return False, self._scrub(f"重启失败：{type(e).__name__}: {e}")

    # ── 内部：设置落库（全部静默兜底）──────────────────────

    def _persist_allowed(self, allowed_users: str | None, allowed_chats: str | None) -> None:
        items: dict[str, str] = {}
        if allowed_users is not None:
            items["tg_allowed_users"] = str(allowed_users).strip()
        if allowed_chats is not None:
            items["tg_allowed_chats"] = str(allowed_chats).strip()
        if not items:
            return
        try:
            _settings().set_many(items)
        except Exception as e:
            log.warning("白名单落库失败（不改运行行为）：%s", type(e).__name__)

    def _persist_token(self, token: str) -> None:
        try:
            _settings().set_many({"tg_token": token})
        except Exception as e:
            log.warning("token 落库失败（本次运行不受影响）：%s", type(e).__name__)

    def _reset_bot_stats(self) -> None:
        bm = self._botmod
        if bm is None:
            return
        try:
            fn = getattr(bm, "reset_update_stats", None)
            if callable(fn):
                fn()
        except Exception:
            pass
