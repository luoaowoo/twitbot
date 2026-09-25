"""发布后端契约（冻结接口 —— 多 agent 并发开发的中枢约定）

所有后端必须实现 `PublishBackend` 协议。**改动本文件前必须同步所有后端实现。**
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


# ── 数据契约 ──────────────────────────────────────────────

@dataclass
class Job:
    """一条待发布任务（由 core.queue 的 sqlite3.Row 转换而来）。"""
    id: int
    kind: str                    # text | photo | video | document
    raw_text: str = ""
    tweet_text: str = ""         # 人工改写后的正文（优先于 raw_text）
    media_path: str = ""         # 相对 MEDIA_DIR 的文件名
    quote_id: str = ""           # 原始推文 id（用于引用转发）
    tg_chat_id: int = 0
    tg_msg_id: int = 0
    attempts: int = 0

    def resolved_media(self, media_dir: Path) -> Path | None:
        """把相对名解析成绝对路径，不存在返回 None。"""
        if not self.media_path:
            return None
        p = Path(self.media_path)
        cand = p if p.is_absolute() else (media_dir / p)
        return cand if cand.exists() else None


@dataclass
class PublishResult:
    """统一返回值。**任何后端、任何失败路径都必须返回它，不得抛异常穿透。**

    ok=False 时 error 必有值；ok=True 时 tweet_url 必有值。
    retryable 指示调用方是否应稍后重试（如限流 True，鉴权失败 False）。
    wait_seconds 供调用方精确等待（如 429 的 rate-limit-reset）。
    """
    ok: bool
    backend: str
    text: str = ""
    tweet_id: str = ""
    tweet_url: str = ""
    error: str = ""
    retryable: bool = False
    wait_seconds: int = 0
    media_uploaded: bool = False
    degraded: str = ""           # 非空表示发生了降级（如媒体权限缺失只发文字）
    extra: dict = field(default_factory=dict)


@runtime_checkable
class PublishBackend(Protocol):
    """所有发布后端的统一接口。"""

    name: str          # "x_api" | "browser"
    label: str         # 人类可读名，Web 控制台展示用

    def available(self) -> tuple[bool, str]:
        """能否工作。返回 (是否可用, 原因说明)。**不得抛异常。**"""
        ...

    def publish(self, job: Job, text: str) -> PublishResult:
        """发布一条。同步阻塞实现即可，调用方负责放线程池。

        text 由调用方（core.compose）算好并传入，后端**不得自行改写正文长度**，
        除非发生降级（降级须在 result.degraded 说明）。
        """
        ...

    def verify(self) -> tuple[bool, str]:
        """轻量连通性/鉴权自检（不产生可见内容）。Web 控制台'测试连接'用。"""
        ...

    def login(self, on_event=None) -> tuple[bool, str]:
        """交互式登录（仅浏览器后端有意义）。

        on_event: 可选回调 on_event(str)，用于把进度推给 Web 控制台。
        阻塞式实现即可。返回 (是否成功, 说明)。
        对无需登录的后端，直接返回 (True, "无需登录")。
        """
        ...


class BackendError(Exception):
    """后端内部异常基类。后端应尽量自行捕获并转成 PublishResult。"""
