#!/usr/bin/env python3
"""Pipeline / Notifier 单元测试（不联网、不碰 X、不碰真实 data/）。

运行：  .venv\\Scripts\\python.exe -m unittest discover -s tests -v
（venv 内未安装 pytest，故用标准库 unittest）

覆盖：成功 / 可重试 / 不可重试 / 后端不可用（attempts 回退）/ publish 抛异常 /
配额上限 / 暂停 / publish_one / run_forever 可取消 / 三种 Notifier。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

# 必须在 import core.* 之前定稿 DATA_DIR —— core.config 在导入期读环境变量
_TMP = Path(tempfile.mkdtemp(prefix="twitbot-test-"))
os.environ.update({
    "TG_TOKEN": "123456:FAKE_TOKEN_FOR_TEST",
    "X_CONSUMER_KEY": "fake", "X_CONSUMER_SECRET": "fake",
    "X_ACCESS_TOKEN": "fake", "X_ACCESS_SECRET": "fake",
    "DATA_DIR": str(_TMP),
    "MODE": "auto",
    "DEDUP_WINDOW": "600",
})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import queue, settings                      # noqa: E402
from core.backends.base import Job, PublishResult     # noqa: E402
from core.notifier import LogNotifier, MultiNotifier, TelegramNotifier  # noqa: E402
from core.pipeline import Pipeline, job_from_row      # noqa: E402

queue.init_db()          # 建表一次；core.queue 的写路径不会自动 init
MSGS = [0]


class NoSleep:
    """不真睡；到次数就 stop（防 run_forever 死循环）。"""

    def __init__(self, stop_after: int | None = None) -> None:
        self.n = 0
        self.stop_after = stop_after
        self.pipe: Pipeline | None = None

    async def __call__(self, seconds: float) -> None:
        await asyncio.sleep(0)
        self.n += 1
        if self.stop_after is not None and self.n >= self.stop_after and self.pipe:
            self.pipe.stop()


class MockBackend:
    """实现 PublishBackend 契约的内存后端。"""
    name = "x_api"
    label = "Mock"

    def __init__(self, ok=True, retryable=False, error="", wait_seconds=0,
                 avail=True, reason="", raises=None):
        self.ok, self.retryable, self.error = ok, retryable, error
        self.wait_seconds, self.avail, self.reason = wait_seconds, avail, reason
        self.raises = raises
        self.calls: list[tuple[int, str]] = []

    def available(self):
        return self.avail, self.reason

    def verify(self):
        return self.avail, self.reason

    def login(self, on_event=None):
        return True, "无需登录"

    def publish(self, job: Job, text: str) -> PublishResult:
        self.calls.append((job.id, text))
        if self.raises is not None:
            raise self.raises
        if self.ok:
            return PublishResult(ok=True, backend=self.name, text=text,
                                 tweet_id=f"t{job.id}", tweet_url=f"https://x.com/i/status/t{job.id}")
        return PublishResult(ok=False, backend=self.name, text=text, error=self.error,
                             retryable=self.retryable, wait_seconds=self.wait_seconds)


def enqueue_one(text: str = "测试正文") -> int:
    """新建一条 pending 并清掉此前遗留的 pending（保证 claim 只取到本用例的）。"""
    with queue.db() as con:
        con.execute("UPDATE jobs SET status='canceled' WHERE status IN ('pending','awaiting')")
    MSGS[0] += 1
    jid = queue.enqueue(tg_chat_id=7, tg_msg_id=MSGS[0], kind="text",
                       raw_text=text, content_hash=f"t{MSGS[0]}")
    assert jid is not None
    return jid


def pipe(backend, sleeper=None) -> Pipeline:
    return Pipeline(backend_resolver=lambda name: backend, sleeper=sleeper or NoSleep())


class PipelineTickTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        queue.init_db()

    def setUp(self):
        settings.set_many({"paused": "0", "monthly_limit": "100000",
                           "tweet_prefix": "", "tweet_suffix": "",
                           "backend": "x_api"})

    def test_success(self):
        be = MockBackend(ok=True)
        jid = enqueue_one("成功用例")
        self.assertTrue(asyncio.run(pipe(be).tick()))
        row = queue.get(jid)
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["tweet_url"], f"https://x.com/i/status/t{jid}")
        self.assertEqual(row["backend"], "x_api")

    def test_retryable_goes_pending_and_consumes_attempt(self):
        jid = enqueue_one("限流用例")
        asyncio.run(pipe(MockBackend(ok=False, retryable=True, error="429")).tick())
        row = queue.get(jid)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 1)

    def test_not_retryable_goes_failed(self):
        jid = enqueue_one("鉴权失败用例")
        asyncio.run(pipe(MockBackend(ok=False, retryable=False, error="401")).tick())
        self.assertEqual(queue.get(jid)["status"], "failed")

    def test_backend_unavailable_rolls_back_attempts(self):
        jid = enqueue_one("后端不可用")
        asyncio.run(pipe(MockBackend(avail=False, reason="未登录")).tick())
        row = queue.get(jid)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 0)          # 关键：不消耗重试次数
        self.assertTrue((row["error"] or "").startswith("后端不可用"))

    def test_backend_load_failure_rolls_back_attempts(self):
        def boom(name):
            raise KeyError(name)
        jid = enqueue_one("后端模块缺失")
        asyncio.run(Pipeline(backend_resolver=boom, sleeper=NoSleep()).tick())
        row = queue.get(jid)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 0)

    def test_publish_exception_is_contained(self):
        be = MockBackend(raises=RuntimeError("底层炸了"))
        p = pipe(be)
        jid = enqueue_one("后端炸")
        self.assertTrue(asyncio.run(p.tick()))         # 主循环不死
        row = queue.get(jid)
        self.assertEqual(row["status"], "pending")     # retryable=True
        self.assertIn("后端异常", row["error"])
        self.assertEqual(p.snapshot()["counters"]["backend_errors"], 1)

    def test_quota_limit_rolls_back_attempts(self):
        settings.set_many({"monthly_limit": "0"})
        jid = enqueue_one("配额用完")
        asyncio.run(pipe(MockBackend(ok=True)).tick())
        row = queue.get(jid)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 0)

    def test_paused_does_not_consume(self):
        settings.set_many({"paused": "1"})
        jid = enqueue_one("暂停中")
        self.assertFalse(asyncio.run(pipe(MockBackend(ok=True)).tick()))
        self.assertEqual(queue.get(jid)["attempts"], 0)

    def test_empty_queue_returns_false(self):
        with queue.db() as con:
            con.execute("UPDATE jobs SET status='canceled' WHERE status='pending'")
        self.assertFalse(asyncio.run(pipe(MockBackend(ok=True)).tick()))

    def test_run_forever_survives_error_then_sends(self):
        class Flaky(MockBackend):
            def __init__(self):
                super().__init__(ok=True)
                self.first = True

            def publish(self, job, text):
                if self.first:
                    self.first = False
                    raise RuntimeError("第一次必炸")
                return super().publish(job, text)

        sl = NoSleep(stop_after=4)
        flaky = Flaky()                       # 同一个实例：第一次炸，第二次成功
        p = Pipeline(backend_resolver=lambda n: flaky, sleeper=sl)
        sl.pipe = p
        jid = enqueue_one("先炸后成功")
        asyncio.run(p.run_forever())
        self.assertEqual(queue.get(jid)["status"], "sent")

    def test_run_forever_is_cancellable(self):
        p = Pipeline(backend_resolver=lambda n: MockBackend(ok=True))

        async def go():
            task = asyncio.create_task(p.run_forever())
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(go())


class PublishOneTest(unittest.TestCase):
    def setUp(self):
        settings.set_many({"paused": "0", "monthly_limit": "100000", "backend": "x_api"})

    def test_publish_one_ok(self):
        jid = enqueue_one("立即发送")
        res = asyncio.run(pipe(MockBackend(ok=True)).publish_one(jid))
        self.assertTrue(res.ok)
        self.assertEqual(queue.get(jid)["status"], "sent")
        self.assertEqual(queue.get(jid)["attempts"], 0)   # 不走 claim

    def test_publish_one_rejects_non_sendable_status(self):
        jid = enqueue_one("已发过的")
        asyncio.run(pipe(MockBackend(ok=True)).publish_one(jid))
        res = asyncio.run(pipe(MockBackend(ok=True)).publish_one(jid))
        self.assertFalse(res.ok)
        self.assertIn("不可立即发送", res.error)

    def test_publish_one_missing_job(self):
        res = asyncio.run(pipe(MockBackend(ok=True)).publish_one(10 ** 9))
        self.assertFalse(res.ok)
        self.assertIn("不存在", res.error)

    def test_publish_one_backend_unavailable(self):
        jid = enqueue_one("后端不可用时立即发送")
        res = asyncio.run(pipe(MockBackend(avail=False, reason="x")).publish_one(jid))
        self.assertFalse(res.ok)
        self.assertTrue(res.retryable)
        self.assertEqual(queue.get(jid)["status"], "pending")


class NotifierTest(unittest.TestCase):
    def setUp(self):
        self.job = Job(id=1, kind="text", raw_text="hi", tg_chat_id=55, tg_msg_id=66)
        self.ok = PublishResult(ok=True, backend="x_api", tweet_url="https://x.com/i/status/1")
        self.bad = PublishResult(ok=False, backend="x_api", error=" boom ")

    def test_log_notifier(self):
        n = LogNotifier()
        asyncio.run(n.sent(self.job, self.ok))
        asyncio.run(n.failed(self.job, self.bad))

    def test_telegram_notifier_without_bot_is_silent(self):
        n = TelegramNotifier()
        self.assertFalse(n.enabled)
        asyncio.run(n.sent(self.job, self.ok))
        asyncio.run(n.failed(self.job, self.bad))

    def test_telegram_notifier_reply_semantics(self):
        sent: list[dict] = []

        class Bot:
            async def send_message(self, **kw):
                sent.append(kw)

        n = TelegramNotifier(Bot())
        asyncio.run(n.sent(self.job, self.ok))
        self.assertEqual(sent[-1]["reply_to_message_id"], 66)
        self.assertTrue(sent[-1]["allow_sending_without_reply"])
        asyncio.run(n.sent(Job(id=2, kind="text", tg_chat_id=0), self.ok))
        self.assertEqual(len(sent), 1)             # Web 投料不回执

    def test_telegram_notifier_swallows_errors(self):
        class Bad:
            async def send_message(self, **kw):
                raise RuntimeError("挂了")

        asyncio.run(TelegramNotifier(Bad()).sent(self.job, self.ok))

    def test_multi_notifier_isolates_failures(self):
        class Bad:
            async def sent(self, job, result):
                raise RuntimeError("x")

            async def failed(self, job, result):
                raise RuntimeError("x")

        m = MultiNotifier([LogNotifier(), Bad()])
        asyncio.run(m.sent(self.job, self.ok))
        asyncio.run(m.failed(self.job, self.bad))

    def test_pipeline_survives_bad_notifier(self):
        class Bad:
            async def sent(self, job, result):
                raise RuntimeError("回执炸了")

            async def failed(self, job, result):
                raise RuntimeError("回执炸了")

        p = Pipeline(notifier=Bad(), backend_resolver=lambda n: MockBackend(ok=True),
                     sleeper=NoSleep())
        jid = enqueue_one("回执炸了也要发出去")
        self.assertTrue(asyncio.run(p.tick()))
        self.assertEqual(queue.get(jid)["status"], "sent")


class ContractTest(unittest.TestCase):
    def test_job_from_row(self):
        settings.set_many({"paused": "0", "monthly_limit": "100000"})
        jid = enqueue_one("契约用例")
        job = job_from_row(queue.get(jid))
        self.assertIsInstance(job, Job)
        self.assertEqual(job.id, jid)
        self.assertEqual(job.kind, "text")
        self.assertIsNone(job.resolved_media(Path(_TMP) / "media"))

    def test_pipeline_defaults(self):
        p = Pipeline()
        self.assertIsInstance(p.notifier, LogNotifier)
        snap = p.snapshot()
        self.assertIn("publishing_job_id", snap)
        self.assertIn("last_result", snap)
        self.assertIn("counters", snap)

    def test_registry_describe_tolerates_missing_backends(self):
        from core.backends import registry
        items = registry.describe()
        self.assertEqual(len(items), 2)
        for it in items:
            self.assertIn("available", it)
            self.assertIn("reason", it)

    def test_bot_module_compiles(self):
        import py_compile
        root = Path(__file__).resolve().parent.parent
        for f in ("bot.py", "core/pipeline.py", "core/notifier.py", "smoke.py"):
            py_compile.compile(str(root / f), doraise=True)


def tearDownModule():
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
