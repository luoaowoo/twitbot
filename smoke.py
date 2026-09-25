#!/usr/bin/env python3
"""集成冒烟测试：用假凭据导入 bot 模块，验证配置加载 / 队列 / 幂等 / 去重 / 裁剪。

不联网、不碰 X。运行：  .venv\\Scripts\\python.exe smoke.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# 必须在 import bot 之前设好假凭据（bot 在导入期读环境变量）
tmp = Path(tempfile.mkdtemp(prefix="twitbot-smoke-"))
os.environ.update({
    "TG_TOKEN": "123456:FAKE_TOKEN_FOR_SMOKE",
    "X_CONSUMER_KEY": "fake", "X_CONSUMER_SECRET": "fake",
    "X_ACCESS_TOKEN": "fake", "X_ACCESS_SECRET": "fake",
    "DATA_DIR": str(tmp),
    "MODE": "confirm",
    "DEDUP_WINDOW": "600",
})

sys.path.insert(0, str(Path(__file__).resolve().parent))
import importlib.util

spec = importlib.util.spec_from_file_location("botmod", Path(__file__).parent / "bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

FAIL = 0


def check(name: str, got, want) -> None:
    global FAIL
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"      got ={got!r}\n      want={want!r}")
        FAIL += 1


print("── 配置层 ──")
check("DATA_DIR 生效", bot.DATA_DIR, tmp)
check("MODE=confirm", bot.MODE, "confirm")
check("媒体目录已建", bot.MEDIA_DIR.is_dir(), True)

print("\n── 队列层 ──")
bot.init_db()
check("init_db 后 DB 文件存在", bot.DB_PATH.exists(), True)

j1 = bot.enqueue(tg_chat_id=111, tg_msg_id=1, kind="text",
                 raw_text="第一条 你好", content_hash="h1")
check("首次入队返回 id", isinstance(j1, int), True)

j1b = bot.enqueue(tg_chat_id=111, tg_msg_id=1, kind="text",
                  raw_text="第一条 你好", content_hash="h1")
check("同 chat+msg 幂等（重投不重复入队）", j1b, None)

j2 = bot.enqueue(tg_chat_id=111, tg_msg_id=2, kind="text",
                 raw_text="第二条", content_hash="h2", status="awaiting")
check("第二条入队", isinstance(j2, int), True)

print("\n── 去重层 ──")
check("窗口内同 hash 判重", bot.is_dup_content("h1"), True)
check("未见过 hash 不判重", bot.is_dup_content("never-seen"), False)
check("空 hash 不判重", bot.is_dup_content(""), False)

print("\n── 出队与状态机 ──")
job = bot.claim_next()
check("claim 只取 pending（不误取 awaiting）", job["id"], j1)
check("claim 后 attempts 递增", job["attempts"], 1)

bot.mark(j1, "sent", tweet_id="999", tweet_url="https://x.com/i/status/999")
again = bot.claim_next()
check("已发送的不会再次出队", again, None)

bot.mark(j2, "pending")
job2 = bot.claim_next()
check("放行后 awaiting 被取出", job2["id"], j2)

print("\n── 配额统计 ──")
check("本月已发计数", bot.month_sent_count(), 1)

print("\n── 回归：反复 429 不会把任务卡死（曾因读到旧行导致永久滞留）──")
# 清空遗留的 pending，确保本段只观察这一条任务
with bot.db() as con:
    con.execute("UPDATE jobs SET status='canceled' WHERE status='pending'")

hr_id = bot.enqueue(tg_chat_id=222, tg_msg_id=99, kind="text", raw_text="会被限流", content_hash="hr")
claim_history = []
for _ in range(bot.MAX_ATTEMPTS + 2):
    j = bot.claim_next()
    if j is None:
        break
    assert j["id"] == hr_id, "串到了其它任务，测试隔离失败"
    claim_history.append(j["attempts"])
    # 模拟 worker 的 429 分支：未达上限则标回 pending
    bot.mark(j["id"], "failed" if j["attempts"] >= bot.MAX_ATTEMPTS else "pending", error="429")

check("attempts 严格递增 1..MAX", claim_history, list(range(1, bot.MAX_ATTEMPTS + 1)))
check("取满上限后不再出队（避免无限重试）", bot.claim_next(), None)
with bot.db() as con:
    st = con.execute("SELECT status FROM jobs WHERE id=?", (hr_id,)).fetchone()["status"]
check("最终状态为 failed", st, "failed")

print("\n── 文案层 ──")
check("中文折算", bot.weighted_len("你好"), 4)
long_text = "标题 " + "内容填充" * 100 + " https://example.com/x"
out = bot.compose("[P] ", long_text, " [S]")
check("超长裁剪 <=280", bot.weighted_len(out) <= 280, True)
check("尾部链接保留", "https://example.com/x" in out, True)

print("\n── 媒体路径解析 ──")
check("空路径返回 None", bot.resolve_media(""), None)
check("不存在的文件返回 None", bot.resolve_media("nope.jpg"), None)
f = bot.MEDIA_DIR / "t.jpg"
f.write_bytes(b"x")
check("相对名解析成功", bot.resolve_media("t.jpg"), f)

print("\n── 单实例锁 ──")
lock = bot.single_instance_lock()
check("首次拿锁成功", lock is not None, True)
# 用子进程模拟「第二个实例」：同进程内 msvcrt 已能拦，但跨进程才是真实场景
import subprocess
probe = Path(__file__).parent / "_lock_child.py"
probe.write_text(
    "import importlib.util,sys,os\n"
    "os.environ.update({'TG_TOKEN':'x','X_CONSUMER_KEY':'x','X_CONSUMER_SECRET':'x',"
    "'X_ACCESS_TOKEN':'x','X_ACCESS_SECRET':'x','DATA_DIR':sys.argv[1]})\n"
    "spec=importlib.util.spec_from_file_location('b',sys.argv[2])\n"
    "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)\n"
    "print('HELD' if m.single_instance_lock() is None else 'FREE')\n",
    encoding="utf-8",
)
r = subprocess.run([sys.executable, str(probe), str(tmp), str(Path(__file__).parent / "bot.py")],
                   capture_output=True, text=True)
verdict = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "ERR"
check("另一进程拿锁被挡（防双发）", verdict, "HELD")
if verdict == "ERR":
    print("      stderr:", r.stderr[-400:])
probe.unlink(missing_ok=True)
lock.close()

print("\n── .env 加载器（Win/Linux 通用关键件）──")
envf = tmp / "test.env"
envf.write_text(
    "# 注释行\n"
    "\n"
    "PLAIN=hello\n"
    "  SPACED  =  padded value  \n"
    'QUOTED="quoted value"\n'
    "SINGLE='single value'\n"
    "export EXPORTED=from_export\n"
    "WITH_EQUALS=a=b=c\n"
    "EMPTYVAL=\n"
    "HAS_HASH=abc#notcomment\n",
    encoding="utf-8",
)
for k in ("PLAIN", "SPACED", "QUOTED", "SINGLE", "EXPORTED", "WITH_EQUALS", "EMPTYVAL", "HAS_HASH"):
    os.environ.pop(k, None)
bot.load_dotenv(envf)
g = os.environ.get
check("普通键", g("PLAIN"), "hello")
check("去空格", g("SPACED"), "padded value")
check("双引号剥离", g("QUOTED"), "quoted value")
check("单引号剥离", g("SINGLE"), "single value")
check("export 前缀", g("EXPORTED"), "from_export")
check("值中带 =", g("WITH_EQUALS"), "a=b=c")
check("空值", g("EMPTYVAL"), "")
check("# 非行首不算注释", g("HAS_HASH"), "abc#notcomment")

# 已存在的真实环境变量优先（容器/CI 覆盖 .env 的语义）
os.environ["PLAIN"] = "from_real_env"
bot.load_dotenv(envf)
check("真实环境变量优先于 .env", g("PLAIN"), "from_real_env")

# BOM 容错（Windows 记事本另存 UTF-8 会加 BOM）
envf.write_text("\ufeffBOMKEY=ok\n", encoding="utf-8")
os.environ.pop("BOMKEY", None)
bot.load_dotenv(envf)
check("UTF-8 BOM 容错", g("BOMKEY"), "ok")

# 文件不存在不应崩
bot.load_dotenv(tmp / "does-not-exist.env")
check("缺文件不抛错", True, True)

print("\n── pipeline：mock 后端全流程 ──")
import asyncio

from core import queue as Q
from core import settings as S
from core.backends.base import Job, PublishResult
from core.notifier import LogNotifier, MultiNotifier, TelegramNotifier
from core.pipeline import Pipeline

# 隔离：把遗留 pending 清掉，后续断言只观察本段新建的任务
with Q.db() as con:
    con.execute("UPDATE jobs SET status='canceled' WHERE status IN ('pending','awaiting')")

_NEXT_MSG = [9000]


def mk_job(text="pipeline 用例") -> int:
    """新建一条 pending 任务，返回 id。

    先清掉上一段遗留的 pending —— claim_next 是按 id 顺序取的，
    不隔离的话会串到前一段的任务上，断言就失去意义。
    """
    with Q.db() as con:
        con.execute("UPDATE jobs SET status='canceled' WHERE status IN ('pending','awaiting')")
    _NEXT_MSG[0] += 1
    return Q.enqueue(tg_chat_id=900, tg_msg_id=_NEXT_MSG[0], kind="text",
                     raw_text=text, content_hash=f"ph{_NEXT_MSG[0]}")


def status_of(jid: int) -> str:
    return Q.get(jid)["status"]


class NoSleep:
    """测试用：不真睡，只记账；够次数就请求 pipeline 停止。"""

    def __init__(self, stop_after: int | None = None) -> None:
        self.n = 0
        self.stop_after = stop_after
        self.pipe: Pipeline | None = None

    async def __call__(self, seconds: float) -> None:
        await asyncio.sleep(0)
        self.n += 1
        if self.stop_after is not None and self.n >= self.stop_after and self.pipe is not None:
            self.pipe.stop()


class MockBackend:
    """实现 PublishBackend 契约的内存后端。"""
    name = "x_api"
    label = "Mock 后端"

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


def run(coro):
    return asyncio.run(coro)


def pipe_with(backend):
    """造一个只认这台 mock 后端的 pipeline。"""
    return Pipeline(backend_resolver=lambda name: backend, sleeper=NoSleep())


# ① 成功路径
ok_be = MockBackend(ok=True)
p = pipe_with(ok_be)
jid = mk_job("要发的正文")
handled = run(p.tick())
row = Q.get(jid)
check("tick 取到任务", handled, True)
check("成功路径 -> sent", row["status"], "sent")
check("tweet_url 落库", row["tweet_url"], f"https://x.com/i/status/t{jid}")
check("backend 名落库", row["backend"], "x_api")
check("成功计数", p.snapshot()["counters"]["sent"], 1)
check("快照含 last_result.ok", p.snapshot()["last_result"]["ok"], True)
check("无前后缀时正文即原文", ok_be.calls[0][1], "要发的正文")

# ①b 前缀/后缀走运行期设置（控制台可改）
S.set_many({"tweet_prefix": "[P] ", "tweet_suffix": " [S]"})
ok_be2 = MockBackend(ok=True)
p2 = pipe_with(ok_be2)
mk_job("带前后缀的正文")
run(p2.tick())
check("运行期前缀参与合成", ok_be2.calls[0][1].startswith("[P] "), True)
check("运行期后缀参与合成", ok_be2.calls[0][1].endswith("[S]"), True)

# ①c 人工改写过的 tweet_text 优先，且不再叠加前后缀
ok_be3 = MockBackend(ok=True)
p3 = pipe_with(ok_be3)
jid = mk_job("原始正文")
with Q.db() as con:
    con.execute("UPDATE jobs SET tweet_text='人工改写过的正文' WHERE id=?", (jid,))
run(p3.tick())
check("tweet_text 优先（不加前后缀）", ok_be3.calls[0][1], "人工改写过的正文")
S.set_many({"tweet_prefix": bot.TWEET_PREFIX, "tweet_suffix": bot.TWEET_SUFFIX})

# ② retryable=True -> 回 pending 且 attempts 递增
retry_be = MockBackend(ok=False, retryable=True, error="429 限流", wait_seconds=0)
p = pipe_with(retry_be)
jid = mk_job("会被限流")
run(p.tick())
row = Q.get(jid)
check("可重试失败 -> pending", row["status"], "pending")
check("attempts 已递增（未回退）", row["attempts"], 1)
check("error 落库", "429" in (row["error"] or ""), True)
check("可重试计数", p.snapshot()["counters"]["retried"], 1)

# ③ retryable=False -> failed
hard_be = MockBackend(ok=False, retryable=False, error="401 鉴权失败")
p = pipe_with(hard_be)
jid = mk_job("会永久失败")
run(p.tick())
row = Q.get(jid)
check("不可重试 -> failed", row["status"], "failed")
check("failed 计数", p.snapshot()["counters"]["failed"], 1)

# ④ 后端不可用 -> pending 且 attempts 被回退（不消耗重试次数）
p = pipe_with(MockBackend(avail=False, reason="未登录"))
jid = mk_job("后端没配好")
run(p.tick())
row = Q.get(jid)
check("后端不可用 -> pending", row["status"], "pending")
check("attempts 被回退（不耗重试次数）", row["attempts"], 0)
check("原因注明后端不可用", (row["error"] or "").startswith("后端不可用"), True)
check("不可用计数", p.snapshot()["counters"]["backend_unavailable"], 1)

# ④b 后端加载抛异常（后端文件还没写出来）-> 同样回退
def _boom(name):
    raise KeyError(f"未知后端: {name!r}")


p = Pipeline(backend_resolver=_boom, sleeper=NoSleep())
jid = mk_job("后端模块缺失")
run(p.tick())
row = Q.get(jid)
check("后端加载失败 -> pending", row["status"], "pending")
check("后端加载失败 -> attempts 回退", row["attempts"], 0)
check("加载失败原因可读", "加载失败" in (row["error"] or ""), True)

# ⑤ publish 抛异常 -> 被兜住，主循环不死
boom_be = MockBackend(raises=RuntimeError("底层炸了"))
p = pipe_with(boom_be)
jid = mk_job("后端会炸")
alive = run(p.tick())
row = Q.get(jid)
check("publish 抛异常被兜住（tick 正常返回）", alive, True)
check("异常 -> 回 pending 重试", row["status"], "pending")
check("异常计入 backend_errors", p.snapshot()["counters"]["backend_errors"], 1)
check("异常文案前半段", (row["error"] or "").startswith("后端异常: RuntimeError"), True)

# ⑤b run_forever：一次异常后主循环继续，下一条照样发出去
class FlakyBackend(MockBackend):
    def __init__(self):
        super().__init__(ok=True)
        self.first = True

    def publish(self, job: Job, text: str):
        if self.first:
            self.first = False
            raise RuntimeError("第一次必炸")
        return super().publish(job, text)


flaky = FlakyBackend()
sl = NoSleep(stop_after=4)
p = Pipeline(backend_resolver=lambda name: flaky, sleeper=sl)
sl.pipe = p
jid = mk_job("先炸后成功")
run(p.run_forever())
check("run_forever 异常后未崩", status_of(jid), "sent")
check("run_forever 可正常退出（stop 生效）", sl.n >= 4, True)

# ⑥ 月度配额达上限 -> pending 且 attempts 回退
S.set_many({"monthly_limit": "0"})
p = pipe_with(MockBackend(ok=True))
jid = mk_job("配额用完了")
run(p.tick())
row = Q.get(jid)
check("配额上限 -> pending", row["status"], "pending")
check("配额上限 -> attempts 回退", row["attempts"], 0)
check("配额计数", p.snapshot()["counters"]["quota_blocked"], 1)
S.set_many({"monthly_limit": str(bot.MONTHLY_LIMIT)})

# ⑦ paused -> 不消耗任务
S.set_many({"paused": "1"})
p = pipe_with(MockBackend(ok=True))
jid = mk_job("暂停期间不该被动")
check("暂停时 tick 返回 False", run(p.tick()), False)
check("暂停时任务未被领取（仍 pending 且 attempts=0）", Q.get(jid)["attempts"], 0)
check("暂停计数", p.snapshot()["counters"]["skipped_paused"], 1)
S.set_many({"paused": "0"})

# ⑧ publish_one：正常发布
p = pipe_with(MockBackend(ok=True))
jid = mk_job("立即发送")
res = run(p.publish_one(jid))
check("publish_one 正常返回 ok", res.ok, True)
check("publish_one 后状态 sent", status_of(jid), "sent")
check("publish_one 不递增 attempts（不走队列）", Q.get(jid)["attempts"], 0)

# ⑨ publish_one：状态不可发 -> ok=False
res = run(p.publish_one(jid))
check("非可发状态返回 ok=False", res.ok, False)
check("非可发状态给出原因", "不可立即发送" in res.error, True)
res = run(p.publish_one(999999))
check("任务不存在返回 ok=False", res.ok, False)
check("不存在原因可读", "不存在" in res.error, True)

# ⑨b publish_one：后端不可用 -> 可重试
p = pipe_with(MockBackend(avail=False, reason="storage_state 过期"))
jid = mk_job("后端不可用时立即发送")
res = run(p.publish_one(jid))
check("publish_one 后端不可用 ok=False", res.ok, False)
check("publish_one 后端不可用 retryable", res.retryable, True)
check("publish_one 未污染任务状态", status_of(jid), "pending")

print("\n── notifier ──")
check("LogNotifier 可用", hasattr(LogNotifier(), "sent"), True)
run(LogNotifier().sent(Job(id=1, kind="text"), PublishResult(ok=True, backend="x_api")))
run(LogNotifier().failed(Job(id=1, kind="text"), PublishResult(ok=False, backend="x_api", error="x")))
check("LogNotifier 不抛异常", True, True)

tb = TelegramNotifier()                      # 未提供 bot
check("TelegramNotifier 无 bot 时可构造", tb.enabled, False)
jid = mk_job("无 bot 回执")
job_obj = Job(id=jid, kind="text", raw_text="x", tg_chat_id=12345, tg_msg_id=1)
run(tb.sent(job_obj, PublishResult(ok=True, backend="x_api", tweet_url="u")))
check("TelegramNotifier 无 bot 静默不抛", True, True)

sent_msgs: list[dict] = []


class FakeBot:
    async def send_message(self, **kw):
        sent_msgs.append(kw)


tb2 = TelegramNotifier(FakeBot())
run(tb2.sent(job_obj, PublishResult(ok=True, backend="x_api", tweet_url="https://x.com/i/status/1")))
check("TelegramNotifier 回执带 reply_to_message_id", sent_msgs[-1]["reply_to_message_id"], 1)
check("TelegramNotifier allow_sending_without_reply", sent_msgs[-1]["allow_sending_without_reply"], True)
run(tb2.failed(job_obj, PublishResult(ok=False, backend="x_api", error="boom")))
check("失败回执也发出", len(sent_msgs), 2)

web_job = Job(id=jid, kind="text", raw_text="x", tg_chat_id=0, tg_msg_id=0)
run(tb2.sent(web_job, PublishResult(ok=True, backend="x_api", tweet_url="u")))
check("Web 投料（chat=0）跳过回执", len(sent_msgs), 2)


class BadBot:
    async def send_message(self, **kw):
        raise RuntimeError("Telegram 挂了")


run(TelegramNotifier(BadBot()).sent(job_obj, PublishResult(ok=True, backend="x_api")))
check("回执发送失败不抛出", True, True)

mn = MultiNotifier([LogNotifier(), TelegramNotifier(BadBot()), object()])
run(mn.sent(job_obj, PublishResult(ok=True, backend="x_api")))
run(mn.failed(job_obj, PublishResult(ok=False, backend="x_api", error="x")))
check("MultiNotifier 单个失败不影响其它", len(mn.notifiers), 3)

pipe_n = Pipeline(notifier=BadBot(), backend_resolver=lambda n: MockBackend(ok=True), sleeper=NoSleep())
jid = mk_job("回执炸了也要发出去")
check("通知器抛异常时发布照常成功", run(pipe_n.tick()), True)
check("通知器异常不影响落库", status_of(jid), "sent")

print("\n── 回归：bot.py 仍可编译 ──")
import py_compile as _pc
try:
    _pc.compile(str(Path(__file__).parent / "bot.py"), doraise=True)
    _pc.compile(str(Path(__file__).parent / "core" / "pipeline.py"), doraise=True)
    _pc.compile(str(Path(__file__).parent / "core" / "notifier.py"), doraise=True)
    check("bot.py / pipeline.py / notifier.py 编译通过", True, True)
except Exception as e:
    check("编译通过", f"{type(e).__name__}: {e}", True)

check("bot.build_application 已导出", callable(bot.build_application), True)
check("Pipeline() 可无参构造", isinstance(Pipeline(), Pipeline), True)
check("Pipeline() 默认为 LogNotifier", isinstance(Pipeline().notifier, LogNotifier), True)
check("registry.describe 容错（后端文件缺失不抛）", isinstance(
    __import__("core.backends.registry", fromlist=["x"]).describe(), list), True)

print("\n── 清理 ──")
import shutil
shutil.rmtree(tmp, ignore_errors=True)
print("已清理临时数据目录")

print(f"\n{'全部通过' if FAIL == 0 else f'{FAIL} 项失败'}")
sys.exit(1 if FAIL else 0)
