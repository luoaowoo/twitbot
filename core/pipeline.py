"""出队 -> 发布 -> 回执 的循环（后端无关，★A）

设计要点：
  * 本模块**不认识**任何具体后端：后端名由 `core.settings.current_backend()` 决定，
    实例由 `core.backends.registry.get(name)` 惰性取得。后端文件缺失/加载失败
    只影响这一条任务的出队，不影响主循环。
  * `publish()` 按契约「绝不抛异常」，但后端是并行开发的，这里**再包一层**兜底：
    任何异常都转成 `PublishResult(retryable=True)`，绝不让底层 bug 崩掉主循环。
  * 后端不可用 / 月度配额已满 时把任务标回 pending 并**回退 attempts**——
    否则一份坏配置会把任务自己的重试次数耗光，等用户到控制台改好配置时任务已经死了。
  * 同步阻塞的 `publish()` 一律丢进 `asyncio.to_thread`，不阻塞长轮询与 Web 控制台。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from . import config, queue, settings, textutil
from .backends import registry
from .backends.base import Job, PublishResult

log = logging.getLogger("twitbot.pipeline")

# 后端不可用时的等待秒数（等用户去 Web 控制台换后端/补登录态）
BACKEND_RETRY_SECONDS = 5
# 月度配额耗尽后的等待秒数
QUOTA_RETRY_SECONDS = 300
# 循环体意外异常后的冷却秒数
LOOP_ERROR_SECONDS = 10

BackendResolver = Callable[[str], Any]
Sleeper = Callable[[float], Any]


def job_from_row(row) -> Job:
    """sqlite3.Row -> Job（缺列/空值一律给安全默认）。"""
    g = (lambda k, d=None: (row[k] if k in row.keys() else d))
    return Job(
        id=int(row["id"]),
        kind=str(row["kind"] or ""),
        raw_text=g("raw_text") or "",
        tweet_text=g("tweet_text") or "",
        media_path=g("media_path") or "",
        quote_id=g("quote_id") or "",
        tg_chat_id=int(g("tg_chat_id") or 0),
        tg_msg_id=int(g("tg_msg_id") or 0),
        attempts=int(g("attempts") or 0),
    )


def result_dict(res: PublishResult, job_id: int | None = None) -> dict:
    """PublishResult -> 可 JSON 化的快照字典（Web SSE / 排障用）。"""
    return {
        "job_id": job_id,
        "ok": bool(res.ok),
        "backend": res.backend,
        "tweet_id": res.tweet_id,
        "tweet_url": res.tweet_url,
        "error": res.error,
        "retryable": bool(res.retryable),
        "wait_seconds": int(res.wait_seconds or 0),
        "media_uploaded": bool(res.media_uploaded),
        "degraded": res.degraded,
        "text": (res.text or "")[:500],
    }


class Pipeline:
    """后端无关的发布循环。

    用法::

        p = Pipeline(notifier=TelegramNotifier(app.bot))
        asyncio.create_task(p.run_forever())      # 常驻
        await p.publish_one(12)                   # Web 控制台「立即发送」

    可注入项（仅供测试，生产用默认值）：
      * `backend_resolver(name) -> backend`：默认 `core.backends.registry.get`
      * `sleeper(seconds) -> awaitable`：默认 `asyncio.sleep`
    """

    def __init__(
        self,
        notifier=None,
        *,
        backend_resolver: BackendResolver | None = None,
        sleeper: Sleeper | None = None,
    ) -> None:
        if notifier is None:
            from .notifier import LogNotifier          # 延迟导入，避免模块级循环依赖
            notifier = LogNotifier()
        self.notifier = notifier
        self._resolve: BackendResolver = backend_resolver or registry.get
        self._sleep: Sleeper = sleeper or asyncio.sleep

        self._stopping = False
        self.publishing_job_id: int | None = None
        # 上次成功发布的时间戳（用于「发送间隔」节流）。0 = 本次运行还没发过。
        self._last_sent_at: float = 0.0
        self.last_result: dict | None = None
        self.started_at: float | None = None
        self.last_error: str = ""
        self.counters: dict[str, int] = {
            "ticks": 0,
            "claimed": 0,
            "sent": 0,
            "failed": 0,
            "retried": 0,
            "degraded": 0,
            "backend_errors": 0,
            "backend_unavailable": 0,
            "quota_blocked": 0,
            "skipped_paused": 0,
            "skipped_empty": 0,
            "loop_errors": 0,
        }

    # ── 运行态快照 ────────────────────────────────────────

    def snapshot(self) -> dict:
        """给 Web 控制台/排障用的运行态快照。"""
        return {
            "publishing_job_id": self.publishing_job_id,
            "last_result": self.last_result,
            "counters": dict(self.counters),
            "backend": settings.current_backend(),
            "paused": settings.is_paused(),
            "stopping": self._stopping,
            "started_at": self.started_at,
            "last_error": self.last_error,
        }

    # ── 主循环 ────────────────────────────────────────────

    def stop(self) -> None:
        """请求优雅退出（下一轮循环结束后返回）。"""
        self._stopping = True

    async def run_forever(self) -> None:
        """常驻循环。可被 `task.cancel()`；异常只记日志，不退出。"""
        if self.started_at is None:
            self.started_at = time.time()
        log.info("pipeline 启动：后端=%s 轮询=%ss",
                 settings.current_backend(), config.POLL_SECONDS)
        while not self._stopping:
            try:
                await self.tick()
            except asyncio.CancelledError:
                log.info("pipeline 被取消，退出")
                raise
            except Exception as e:                     # 兜底：循环体绝不崩
                self.counters["loop_errors"] += 1
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("pipeline 循环异常")
                await self._sleep(LOOP_ERROR_SECONDS)
        log.info("pipeline 已停止")

    async def tick(self) -> bool:
        """处理一条任务；返回是否取到了任务（测试用）。

        暂停 / 队列为空 -> False（不消耗任务）。
        取到任务后无论成败都返回 True（该任务本轮已有终局：sent/failed/pending）。
        """
        self.counters["ticks"] += 1

        if settings.is_paused():
            self.counters["skipped_paused"] += 1
            await self._sleep(config.POLL_SECONDS)
            return False

        # 队列自动发送开关：关掉时只入库不发，等用户手动逐条点「发送」。
        # （与 paused 的区别：paused 是临时暂停，这个是"我就要手动发"的长期偏好）
        try:
            if not settings.queue_send_enabled():
                self.counters["skipped_queue_off"] = (
                    self.counters.get("skipped_queue_off", 0) + 1)
                await self._sleep(config.POLL_SECONDS)
                return False
        except Exception:
            pass

        # 发送间隔节流：距上次成功发布不足设定间隔就先等一会儿。
        # 这是防连发被风控的软节流 —— 注意只是"延后"，不是丢弃。
        try:
            gap = settings.effective_send_interval_seconds()
        except Exception:
            gap = 0
        if gap > 0 and self._last_sent_at > 0:
            waited = time.time() - self._last_sent_at
            if waited < gap:
                self.counters["throttled"] = self.counters.get("throttled", 0) + 1
                await self._sleep(min(gap - waited, 60.0))
                return False

        row = queue.claim_next()
        if row is None:
            self.counters["skipped_empty"] += 1
            await self._sleep(config.POLL_SECONDS)
            return False

        self.counters["claimed"] += 1
        return await self._handle(row)

    # ── 单条处理 ──────────────────────────────────────────

    async def _handle(self, row) -> bool:
        job_id = int(row["id"])
        try:
            backend, why = self._load_backend()
            if backend is None:
                # 关键：回退 attempts —— 坏配置不该耗光任务的重试次数
                self.counters["backend_unavailable"] += 1
                self._release(job_id, row, f"后端不可用: {why}")
                log.warning("#%s 后端不可用，任务标回 pending 等配置修复：%s", job_id, why)
                await self._sleep(BACKEND_RETRY_SECONDS)
                return True

            name = self._backend_name(backend)
            text = self._compose(row)

            if queue.month_sent_count() >= settings.effective_monthly_limit():
                self.counters["quota_blocked"] += 1
                self._release(job_id, row, "月度配额已达上限")
                log.warning("月度配额 %s 已达上限，任务 #%s 标回 pending",
                            settings.effective_monthly_limit(), job_id)
                await self._sleep(QUOTA_RETRY_SECONDS)
                return True

            job = job_from_row(row)
            self.publishing_job_id = job_id
            try:
                res = await self._publish(backend, job, text)
            finally:
                self.publishing_job_id = None
            self.last_result = result_dict(res, job_id)

            if res.ok:
                queue.mark(job_id, "sent", tweet_id=res.tweet_id,
                           tweet_url=res.tweet_url, backend=name, error="")
                self.counters["sent"] += 1
                if res.degraded:
                    self.counters["degraded"] += 1
                    log.warning("#%s 已发布（降级：%s）-> %s", job_id, res.degraded, res.tweet_url)
                else:
                    log.info("#%s 已发布 -> %s", job_id, res.tweet_url)
                # 记下成功时间，供「发送间隔」节流使用
                self._last_sent_at = time.time()
                await self._notify("sent", job, res)
            elif res.retryable and job.attempts < config.MAX_ATTEMPTS:
                queue.mark(job_id, "pending", error=res.error or "可重试错误", backend=name)
                self.counters["retried"] += 1
                log.warning("#%s 发布失败（可重试，attempts=%s/%s）：%s",
                            job_id, job.attempts, config.MAX_ATTEMPTS, res.error)
                if res.wait_seconds > 0:                # 精确等待（如 429 rate-limit-reset）
                    log.warning("#%s 等待 %ss 后重试", job_id, res.wait_seconds)
                    await self._sleep(res.wait_seconds)
            else:
                queue.mark(job_id, "failed", error=res.error or "发布失败", backend=name)
                self.counters["failed"] += 1
                log.error("#%s 发布失败（不可重试或已达上限）：%s", job_id, res.error)
                await self._notify("failed", job, res)
            return True

        except asyncio.CancelledError:
            raise
        except Exception as e:                          # 兜底：绝不让主循环崩
            self.counters["loop_errors"] += 1
            self.last_error = f"#{job_id} {type(e).__name__}: {e}"
            log.exception("#%s 处理时发生意外异常", job_id)
            try:
                self._release(job_id, row, f"pipeline 内部异常: {type(e).__name__}: {e}")
            except Exception:
                log.exception("#%s 回退任务状态也失败", job_id)
            return True

    # ── Web 控制台「立即发送」────────────────────────────

    async def publish_one(self, job_id: int) -> PublishResult:
        """同步发布单条（不走 claim 队列，不递增 attempts）。

        * 任务不存在 -> ok=False，「任务 #N 不存在」
        * 状态不是 pending/failed/awaiting -> ok=False，附当前状态说明
        * 后端不可用 -> ok=False，error 以「后端不可用:」开头，retryable=True
        * 无论成败都落库（sent / pending / failed），与主循环口径一致
        """
        backend_name = settings.current_backend()
        try:
            row = queue.get(int(job_id))
        except Exception as e:
            return PublishResult(ok=False, backend=backend_name,
                                 error=f"读取任务失败: {type(e).__name__}: {e}")
        if row is None:
            return PublishResult(ok=False, backend=backend_name,
                                 error=f"任务 #{job_id} 不存在")
        status = str(row["status"] or "")
        if status not in ("pending", "failed", "awaiting"):
            return PublishResult(ok=False, backend=backend_name,
                                 error=f"任务 #{job_id} 当前状态为 {status}，不可立即发送")

        backend, why = self._load_backend()
        if backend is None:
            return PublishResult(ok=False, backend=backend_name,
                                 error=f"后端不可用: {why}", retryable=True)

        name = self._backend_name(backend)
        text = self._compose(row)
        job = job_from_row(row)
        self.publishing_job_id = job.id
        try:
            res = await self._publish(backend, job, text)
        finally:
            self.publishing_job_id = None
        self.last_result = result_dict(res, job.id)

        try:
            if res.ok:
                queue.mark(job.id, "sent", tweet_id=res.tweet_id,
                           tweet_url=res.tweet_url, backend=name, error="")
                self.counters["sent"] += 1
                if res.degraded:
                    self.counters["degraded"] += 1
                self._last_sent_at = time.time()
                await self._notify("sent", job, res)
            elif res.retryable and job.attempts < config.MAX_ATTEMPTS:
                queue.mark(job.id, "pending", error=res.error or "可重试错误", backend=name)
                self.counters["retried"] += 1
            else:
                queue.mark(job.id, "failed", error=res.error or "发布失败", backend=name)
                self.counters["failed"] += 1
                await self._notify("failed", job, res)
        except Exception:
            log.exception("#%s 立即发送的结果落库失败", job.id)
        return res

    # ── 内部工具 ─────────────────────────────────────────

    def _compose(self, row) -> str:
        """人工改写过的 tweet_text 优先；否则用运行期前缀/后缀合成。"""
        try:
            return row["tweet_text"] or textutil.compose(
                settings.effective_prefix(), row["raw_text"] or "", settings.effective_suffix()
            )
        except Exception:
            log.exception("#%s 正文合成失败，退化为原文", row["id"] if "id" in row.keys() else "?")
            return (row["raw_text"] or "")[:280]

    def _load_backend(self) -> tuple[Any | None, str]:
        """取后端并做可用性检查。返回 (后端, 失败原因)；失败时后端为 None。"""
        name = (settings.current_backend() or "").strip().lower()
        try:
            backend = self._resolve(name)
        except Exception as e:
            return None, f"加载失败({name}): {type(e).__name__}: {e}"
        if backend is None:
            return None, f"未注册: {name}"
        try:
            ok, reason = backend.available()
        except Exception as e:
            return None, f"available() 异常({name}): {type(e).__name__}: {e}"
        if not ok:
            return None, f"{name}: {reason or '未说明原因'}"
        return backend, ""

    @staticmethod
    def _backend_name(backend) -> str:
        return str(getattr(backend, "name", "") or settings.current_backend())

    async def _publish(self, backend, job: Job, text: str) -> PublishResult:
        """调用后端（线程池）+ 防御性兜底。**任何情况下都返回 PublishResult。**"""
        name = self._backend_name(backend)
        try:
            raw = await asyncio.to_thread(backend.publish, job, text)
        except Exception as e:
            self.counters["backend_errors"] += 1
            log.exception("#%s 后端 %s publish 抛出异常（已兜住）", job.id, name)
            return PublishResult(ok=False, backend=name, text=text,
                                 error=f"后端异常: {type(e).__name__}: {e}", retryable=True)
        return self._normalize(raw, name, text)

    @staticmethod
    def _normalize(raw, name: str, text: str) -> PublishResult:
        """防御：后端返回非 PublishResult（None/dict/鸭子类型）时统一成契约对象。"""
        if isinstance(raw, PublishResult):
            if not raw.backend:
                raw.backend = name
            if not raw.text:
                raw.text = text
            return raw
        try:
            ok = bool(getattr(raw, "ok"))
        except Exception:
            return PublishResult(ok=False, backend=name, text=text,
                                 error=f"后端返回非法结果: {type(raw).__name__}", retryable=True)

        def g(k, d):
            try:
                v = getattr(raw, k)
            except Exception:
                return d
            return d if v is None else v

        return PublishResult(
            ok=ok,
            backend=str(g("backend", "") or name),
            text=str(g("text", "") or text),
            tweet_id=str(g("tweet_id", "") or ""),
            tweet_url=str(g("tweet_url", "") or ""),
            error=str(g("error", "") or ""),
            retryable=bool(g("retryable", False)),
            wait_seconds=int(g("wait_seconds", 0) or 0),
            media_uploaded=bool(g("media_uploaded", False)),
            degraded=str(g("degraded", "") or ""),
            extra=g("extra", {}) if isinstance(g("extra", {}), dict) else {},
        )

    def _release(self, job_id: int, row, reason: str) -> None:
        """把任务标回 pending 并回退 attempts（不消耗重试次数）。"""
        attempts = int(row["attempts"] or 0) if row is not None else 0
        queue.mark(job_id, "pending", attempts=max(0, attempts - 1), error=reason)

    async def _notify(self, event: str, job: Job, res: PublishResult) -> None:
        """回执通知。通知失败只记警告，绝不影响发布流程。"""
        try:
            if event == "sent":
                await self.notifier.sent(job, res)
            else:
                await self.notifier.failed(job, res)
        except Exception:
            log.warning("#%s 回执通知失败（忽略）", job.id, exc_info=True)
