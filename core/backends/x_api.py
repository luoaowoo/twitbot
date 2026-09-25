"""X（Twitter）官方 API 发布后端（★B）—— 冻结契约见 AGENT_CONTRACT.md §1。

发布流程（与原 bot.py 的 x_clients()/publish() 等价，但更稳）：
  1. v1.1 ``media/upload`` 上传媒体（免费层常被 403 砍掉 → **降级为纯文本**，
     绝不因此让整条任务失败）
  2. v2 ``create_tweet`` 发推，可选 ``quote_tweet_id`` 引用转发

硬性约定：
  * ``publish()`` / ``available()`` / ``verify()`` **绝不抛异常**，失败一律返回
    ``PublishResult(ok=False, ...)`` / ``(False, 原因)``。
  * 正文由调用方算好传入 ``text``，本后端**不裁剪**（仅媒体失败时降级为纯文本，
    降级原因写入 ``result.degraded``）。
  * 失败分类决定调用方是否重试：见 ``_classify_exception`` 的映射表。
  * ``available()`` **不联网**（Web 控制台会频繁调用）。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

import tweepy

from .base import Job, PublishResult
from .. import config, settings

log = logging.getLogger("twitbot.backend.x_api")

# ── 常量 ──────────────────────────────────────────────────

#: 拿不到 x-rate-limit-reset 头时的兜底等待秒数（X 的 15 分钟窗口）
DEFAULT_RATE_LIMIT_WAIT = 900

#: 429 时最少等待秒数（即便 reset 头已过期也别立刻重试）
MIN_RATE_LIMIT_WAIT = 60

_NO_CREDS_REASON = "未配置 X API 凭据（缺少 X_CONSUMER_KEY 等），请运行 setup_x.py"

_AUTH_HINT = "检查 App 权限是否 Read and Write，且 Access Token 需在改权限后重新生成"

#: 网络类异常（requests / httpx 可能未安装，惰性收集）
_NETWORK_EXC: tuple[type[BaseException], ...] = ()


def _collect_network_exceptions() -> tuple[type[BaseException], ...]:
    found: list[type[BaseException]] = []
    try:
        import requests

        found.append(requests.exceptions.RequestException)
        found.append(requests.exceptions.Timeout)
        found.append(requests.exceptions.ConnectionError)
    except Exception:  # pragma: no cover - requests 是 tweepy 的依赖，几乎不会缺
        pass
    try:
        import httpx

        found.append(httpx.HTTPError)
        found.append(httpx.TimeoutException)
    except Exception:  # pragma: no cover - httpx 可能未安装
        pass
    return tuple(found)


_NETWORK_EXC = _collect_network_exceptions()


# ── 响应/异常解析小工具 ────────────────────────────────────

def _header_value(exc: BaseException, name: str) -> str | None:
    """从 tweepy 异常的 response.headers 里取一个头（大小写不敏感地兜底）。"""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if not headers:
        return None
    for key in (name, name.lower(), name.upper(), name.title()):
        try:
            value = headers.get(key)
        except Exception:
            continue
        if value:
            return str(value)
    return None


def _error_body(exc: BaseException) -> str:
    """把异常里能拿到的原始响应体/错误消息拼成一小段小写文本，用于关键词判定。"""
    parts: list[str] = []
    for msg in list(getattr(exc, "api_messages", None) or []):
        parts.append(str(msg))
    for err in list(getattr(exc, "api_errors", None) or []):
        if isinstance(err, dict):
            parts.extend(str(v) for v in err.values())
        else:
            parts.append(str(err))
    resp = getattr(exc, "response", None)
    if resp is not None:
        for attr in ("text", "reason"):
            value = getattr(resp, attr, None)
            if value:
                parts.append(str(value))
    parts.append(str(exc))
    return "\n".join(parts).lower()


def _rate_limit_wait(exc: BaseException) -> int:
    """429 的等待秒数：优先 tweepy 解析的 reset_time，其次响应头，最后兜底 900。"""
    reset: Any = getattr(exc, "reset_time", None)
    source = "reset_time"
    if reset is None:
        reset = _header_value(exc, "x-rate-limit-reset")
        source = "x-rate-limit-reset 头"
    if reset is None:
        log.warning("429 未拿到限流重置时间，兜底等待 %ss", DEFAULT_RATE_LIMIT_WAIT)
        return DEFAULT_RATE_LIMIT_WAIT
    try:
        wait = int(float(reset)) - int(time.time())
    except (TypeError, ValueError):
        log.warning("限流重置时间无法解析（%r），兜底等待 %ss", reset, DEFAULT_RATE_LIMIT_WAIT)
        return DEFAULT_RATE_LIMIT_WAIT
    wait = max(MIN_RATE_LIMIT_WAIT, wait)
    log.info("429 限流：按 %s 计算需等待 %ss", source, wait)
    return wait


def _classify_exception(exc: BaseException) -> tuple[str, bool, int]:
    """异常 → (面向用户的 error, retryable, wait_seconds)。

    这是调用方「是否重试」的唯一依据，务必保持与 AGENT_CONTRACT.md §1 一致：

    | 异常                              | retryable | wait_seconds                    |
    |-----------------------------------|-----------|---------------------------------|
    | TooManyRequests (429)             | True      | reset - now（≥60），无头则 900  |
    | Unauthorized (401)                | False     | 0                               |
    | Forbidden (403) + duplicate       | False     | 0（改文案，重试无用）           |
    | Forbidden (403) 其它              | False     | 0                               |
    | TwitterServerError (5xx)          | True      | 0                               |
    | 网络类（requests/httpx 超时/连接）| True      | 0                               |
    | 其它 TweepyException              | False     | 0（error 带异常类型名）         |
    | 未知异常                          | False     | 0（error 带异常类型名）         |
    """
    if isinstance(exc, tweepy.TooManyRequests):
        wait = _rate_limit_wait(exc)
        return (f"触发 X 限流（429），约 {wait} 秒后可重试", True, wait)

    if isinstance(exc, tweepy.Unauthorized):
        # 最常见的坑：App 权限仍是 Read，或改权限前生成的 Access Token 没重发
        return (f"鉴权失败（401）：{_AUTH_HINT}", False, 0)

    if isinstance(exc, tweepy.Forbidden):
        body = _error_body(exc)
        if "duplicate" in body:
            return ("内容与近期推文重复，X 直接拒绝，请改文案", False, 0)
        return (
            "发布被拒（403）：权限或配额问题（免费层无媒体上传权限 / 已达配额），"
            f"{_AUTH_HINT}",
            False,
            0,
        )

    if isinstance(exc, tweepy.TwitterServerError):
        return (f"X 服务端错误（{type(exc).__name__}），稍后重试", True, 0)

    if _NETWORK_EXC and isinstance(exc, _NETWORK_EXC):
        return (f"网络错误（{type(exc).__name__}）：{exc}", True, 0)

    if isinstance(exc, tweepy.TweepyException):
        return (f"X API 调用失败：{type(exc).__name__}: {exc}", False, 0)

    return (f"未预期异常 {type(exc).__name__}: {exc}", False, 0)


def _describe_media_failure(exc: BaseException) -> str:
    """媒体上传失败的简短原因（写进 result.degraded）。"""
    if isinstance(exc, tweepy.Forbidden):
        return "403 无 media/upload 权限（免费层常见）"
    if isinstance(exc, tweepy.Unauthorized):
        return f"401 鉴权失败，{_AUTH_HINT}"
    if isinstance(exc, tweepy.TooManyRequests):
        return "429 媒体上传被限流"
    if isinstance(exc, tweepy.TwitterServerError):
        return "X 服务端错误，媒体上传未完成"
    if _NETWORK_EXC and isinstance(exc, _NETWORK_EXC):
        return f"网络错误（{type(exc).__name__}）"
    msg = " ".join(str(exc).split())
    if len(msg) > 200:
        msg = msg[:200] + "…"
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def _extract_tweet_id(resp: Any) -> str:
    """从 create_tweet 的响应里取推文 id（dict 或对象两种形态）。"""
    data = getattr(resp, "data", None)
    if data is None:
        return ""
    tid = data.get("id") if isinstance(data, dict) else getattr(data, "id", None)
    return str(tid) if tid else ""


def _safe_resolved_media(job: Job) -> Any:
    """解析媒体绝对路径；任何异常都当成"没有媒体"。"""
    try:
        return job.resolved_media(config.MEDIA_DIR)
    except Exception as e:  # 路径异常不应影响发推
        log.warning("#%s 解析媒体路径失败：%s", getattr(job, "id", "?"), e)
        return None


def _quote_target(job: Job) -> str:
    """需要引用转发时返回 quote id，否则空串（quote_mode != auto 时不引用）。"""
    quote_id = str(getattr(job, "quote_id", "") or "").strip()
    if not quote_id:
        return ""
    try:
        mode = (settings.get("quote_mode", config.QUOTE_MODE) or "").strip().lower()
    except Exception as e:
        # DB 不可用不该阻断发布：退回静态配置
        log.warning("读取 quote_mode 失败，回退 .env 配置：%s", e)
        mode = (config.QUOTE_MODE or "").strip().lower()
    return quote_id if mode == "auto" else ""


# ── 后端实现 ──────────────────────────────────────────────

class XApiBackend:
    """X 官方 API 后端（tweepy v2 发推 + v1.1 传媒体）。"""

    name = "x_api"
    label = "X 官方 API"

    def __init__(self) -> None:
        # 客户端构造有开销，做实例级懒加载缓存；publish 在线程池里跑 → 加锁
        self._lock = threading.Lock()
        self._v2: tweepy.Client | None = None
        self._v1: tweepy.API | None = None
        self._v2_creds: tuple[str, str, str, str] | None = None
        self._v1_creds: tuple[str, str, str, str] | None = None

    # ── 客户端缓存 ────────────────────────────────────────

    @staticmethod
    def _creds() -> tuple[str, str, str, str]:
        return (
            config.X_CONSUMER_KEY,
            config.X_CONSUMER_SECRET,
            config.X_ACCESS_TOKEN,
            config.X_ACCESS_SECRET,
        )

    def _v2_client(self) -> tweepy.Client:
        """v2 客户端（发推/读账号）。凭据变了自动重建。"""
        creds = self._creds()
        if self._v2 is None or self._v2_creds != creds:
            with self._lock:
                if self._v2 is None or self._v2_creds != creds:
                    ck, cs, at, asec = creds
                    self._v2 = tweepy.Client(
                        consumer_key=ck,
                        consumer_secret=cs,
                        access_token=at,
                        access_token_secret=asec,
                    )
                    self._v2_creds = creds
                    log.debug("已构建 tweepy.Client（v2）")
        return self._v2

    def _v1_client(self) -> tweepy.API:
        """v1.1 客户端（媒体上传）。凭据变了自动重建。"""
        creds = self._creds()
        if self._v1 is None or self._v1_creds != creds:
            with self._lock:
                if self._v1 is None or self._v1_creds != creds:
                    ck, cs, at, asec = creds
                    auth = tweepy.OAuth1UserHandler(ck, cs, at, asec)
                    self._v1 = tweepy.API(auth)
                    self._v1_creds = creds
                    log.debug("已构建 tweepy.API（v1.1）")
        return self._v1

    def reset_clients(self) -> None:
        """丢弃缓存客户端（凭据被外部改动后调用；测试也用它）。"""
        with self._lock:
            self._v2 = None
            self._v1 = None
            self._v2_creds = None
            self._v1_creds = None

    # ── 契约方法 ──────────────────────────────────────────

    def available(self) -> tuple[bool, str]:
        """有凭据即认为可工作。**不联网**（Web 控制台频繁调用）。"""
        try:
            if not config.has_x_api_creds():
                return False, _NO_CREDS_REASON
            return True, "已配置 API 凭据"
        except Exception as e:  # 契约：不得抛异常
            return False, f"检查凭据时出错：{type(e).__name__}: {e}"

    def verify(self) -> tuple[bool, str]:
        """读自己的账号确认鉴权。**只读，不产生可见内容。**"""
        try:
            ok, reason = self.available()
            if not ok:
                return False, reason
            me = self._v2_client().get_me(user_auth=True)
            data = getattr(me, "data", None)
            username = ""
            if isinstance(data, dict):
                username = str(data.get("username") or "")
            elif data is not None:
                username = str(getattr(data, "username", "") or "")
            if not username:
                return False, "鉴权响应异常：未返回账号信息"
            return True, f"鉴权正常，账号 @{username}"
        except Exception as e:
            msg, _retryable, _wait = _classify_exception(e)
            log.warning("verify 失败：%s", msg)
            return False, msg

    def login(self, on_event: Callable[[str], None] | None = None) -> tuple[bool, str]:
        """官方 API 走凭据鉴权，无交互登录。"""
        try:
            if on_event is not None:
                try:
                    on_event("X 官方 API 使用 API 凭据鉴权，无需交互登录")
                except Exception:
                    pass
            return True, "无需登录（使用 API 凭据）"
        except Exception as e:  # 回调异常也不外抛
            return False, f"{type(e).__name__}: {e}"

    def publish(self, job: Job, text: str) -> PublishResult:
        """发布一条。**任何失败路径都返回 PublishResult，绝不抛异常。**"""
        text = "" if text is None else str(text)
        degraded = ""
        try:
            ok, reason = self.available()
            if not ok:
                return PublishResult(ok=False, backend=self.name, text=text, error=reason)

            # 1) 媒体：失败只降级，不整体失败
            media_ids, degraded = self._upload_media(job)

            # 2) 正文
            kwargs: dict[str, Any] = {"text": text}
            if media_ids:
                kwargs["media_ids"] = media_ids
            quote_id = _quote_target(job)
            if quote_id:
                kwargs["quote_tweet_id"] = quote_id

            log.info(
                "#%s 经 X API 发布（媒体 %d 个%s）",
                getattr(job, "id", "?"),
                len(media_ids),
                "，已降级" if degraded else "",
            )
            resp = self._v2_client().create_tweet(**kwargs)

            tweet_id = _extract_tweet_id(resp)
            if not tweet_id:
                return PublishResult(
                    ok=False,
                    backend=self.name,
                    text=text,
                    error="发布响应异常：未返回推文 id",
                    retryable=False,
                    media_uploaded=bool(media_ids),
                    degraded=degraded,
                )

            return PublishResult(
                ok=True,
                backend=self.name,
                text=text,
                tweet_id=tweet_id,
                tweet_url=f"https://x.com/i/status/{tweet_id}",
                media_uploaded=bool(media_ids),
                degraded=degraded,
                extra={"quote_tweet_id": quote_id} if quote_id else {},
            )
        except Exception as e:
            error, retryable, wait = _classify_exception(e)
            log.warning(
                "#%s 发布失败（retryable=%s wait=%s）：%s",
                getattr(job, "id", "?"),
                retryable,
                wait,
                error,
            )
            return PublishResult(
                ok=False,
                backend=self.name,
                text=text,
                error=error,
                retryable=retryable,
                wait_seconds=wait,
                degraded=degraded,
                extra={"exception": type(e).__name__},
            )

    # ── 内部：媒体上传（失败即降级） ──────────────────────

    def _upload_media(self, job: Job) -> tuple[list[Any], str]:
        """返回 (media_ids, degraded)。任何失败都降级为 []，不抛异常。"""
        path = _safe_resolved_media(job)
        if path is None:
            return [], ""
        try:
            kind = str(getattr(job, "kind", "") or "").lower()
            # 视频体积大，必须分片上传（INIT/APPEND/FINALIZE）
            media = self._v1_client().media_upload(str(path), chunked=(kind == "video"))
            media_id = getattr(media, "media_id", None) or getattr(media, "id", None)
            if not media_id:
                return [], "媒体上传未返回 media_id，已降级为纯文本"
            return [media_id], ""
        except Exception as e:
            reason = _describe_media_failure(e)
            log.warning(
                "#%s 媒体上传失败，降级为纯文本：%s",
                getattr(job, "id", "?"),
                reason,
            )
            return [], f"媒体上传失败，已降级为纯文本：{reason}"
