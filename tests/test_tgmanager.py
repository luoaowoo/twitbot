#!/usr/bin/env python3
"""TelegramManager 单元测试（★E）—— 不联网、不连 Telegram、不碰真实 data/。

覆盖（CONTRACT_V2.md §2.2 硬性要求）：
  * import 期零副作用（子进程 import，不发任何网络请求、不依赖凭据）
  * status() 字段齐全 + **token 绝不泄漏**（完整 token 不出现在任何对外返回值/文案里）
  * 幂等：重复 start() 返回 (True, "机器人已在运行")，不重复起轮询
  * 缺 token / 无效 token(401) → (False, 中文原因)，**不抛异常**
  * token 有效 → 顺手拿到 bot_username / bot_id
  * stop()：未运行时 (True, "机器人未在运行")；运行中能干净收尾
  * restart() = stop() + start()
  * verify_token()：只校验、不启动轮询；失败不抛异常
  * 缺 python-telegram-bot 依赖 → 优雅中文报错，不崩
  * 构造出的 Application 与 bot.py 的 handler 注册一致（复用 build_application）

隔离手段：把 `core.config.TG_TOKEN` 置空 + 把 settings 的 tg_* 键清空，
再往 `core.tgmanager._BOT_MODULE` 里注入**假 bot 模块**（带假 Application）。
这样跑的是本模块的真实逻辑，但不碰 PTB、不碰网络。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# 必须在 import core.* 之前定稿路径/凭据（core.config 在导入期读环境变量）
_TMP = Path(tempfile.mkdtemp(prefix="twitbot-tgmanager-"))
os.environ.update({
    "TG_TOKEN": "",
    "DATA_DIR": str(_TMP),
    "PYTHONIOENCODING": "utf-8",
})
for _k in ("X_CONSUMER_KEY", "X_CONSUMER_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET"):
    os.environ[_k] = ""

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import queue, settings                    # noqa: E402
from core import tgmanager as tgm                   # noqa: E402
from core.tgmanager import TelegramManager, mask_token  # noqa: E402

queue.init_db()

# 真 token 形状（脱敏测试用；其实只是字符串，不会发出去）
GOOD_TOKEN = "123456789:AAHfakeTokenForUnitTest_xyzABC"
BAD_TOKEN = "123456789:AAHdefinitelyRevoked_xyzABC"


# ══════════════════════════════════════════════════════════
# 假 Application / 假 bot 模块
# ══════════════════════════════════════════════════════════

class FakeUpdater:
    def __init__(self, app):
        self.app = app
        self.running = False
        self.polling_starts = 0

    async def start_polling(self, **kw):
        if self.app.fail_at == "polling":
            raise RuntimeError("polling boom")
        if self.app.fail_at == "conflict":
            raise FakeConflict("Conflict: terminated by other getUpdates request")
        self.polling_starts += 1
        self.running = True
        return None

    async def stop(self):
        if self.app.fail_at == "updater_stop":
            raise RuntimeError("updater stop boom")
        if not self.running:
            # 模拟 PTB 的真实行为：未运行就报错
            raise RuntimeError("This Updater is not running!")
        self.running = False


class FakeBot:
    def __init__(self, username="my_test_bot", bid=4242):
        self.username = username
        self.id = bid


class FakeApplication:
    """字段与 PTB Application 对齐到本模块实际用到的那些。"""

    def __init__(self, token="", username="my_test_bot", bid=4242,
                 fail_at="", fail_with=None):
        self.bot = FakeBot(username, bid)
        self.updater = FakeUpdater(self)
        self.running = False
        self.initialized = False
        self.inits = 0
        self.stops = 0
        self.shutdowns = 0
        self.token = token
        self.fail_at = fail_at
        self.fail_with = fail_with

    async def initialize(self):
        self.inits += 1
        if self.fail_at in ("init", "token", "bad_token"):
            if self.fail_with is not None:
                raise self.fail_with
            raise FakeInvalidToken(
                f"The token `{self.token}` was rejected by the server.")
        self.initialized = True

    async def start(self):
        if self.fail_at == "start":
            raise RuntimeError("app start boom")
        self.running = True

    async def stop(self):
        if self.fail_at == "app_stop":
            raise RuntimeError("app stop boom")
        self.running = False

    async def shutdown(self):
        self.shutdowns += 1
        if self.fail_at == "app_shutdown":
            raise RuntimeError("app shutdown boom")
        self.initialized = False


class FakeInvalidToken(Exception):
    """名字与 telegram.error.InvalidToken 一致 —— 本模块按**类名**映射人话。"""


class FakeConflict(Exception):
    """名字与 telegram.error.Conflict 一致。"""


class FakeNetworkError(Exception):
    """名字与 telegram.error.NetworkError 一致。"""


class FakeBotModule:
    """假 `bot` 模块：只提供 TelegramManager 真正会调的东西。"""

    def __init__(self, app_factory=None):
        self.apps: list[FakeApplication] = []
        self.calls: list[str] = []
        self._app_factory = app_factory or (lambda token: FakeApplication(token=token))
        self._updates = 0
        self._last = 0.0

    def build_application(self, token=None):
        if token is None:
            raise SystemExit("[config] 缺少 TG_TOKEN（Telegram 功能需要）。")
        self.calls.append(f"build_application(token={token})")
        app = self._app_factory(token)
        self.apps.append(app)
        return app

    def updates_seen(self):
        self._updates += 1
        return self._updates

    def last_update_at(self):
        self._last += 100.0
        return self._last

    def reset_update_stats(self):
        self._updates = 0
        self._last = 0.0


def run(coro):
    return asyncio.run(coro)


class TgManagerTest(unittest.TestCase):
    """所有用例共用：清空凭据/设置 + 注入假 bot 模块。"""

    def setUp(self) -> None:
        self._saved_token = getattr(tgm, "_BOT_MODULE", None)
        self._saved_cfg_token = None
        from core import config
        self._saved_cfg_token = config.TG_TOKEN
        config.TG_TOKEN = ""
        # 清掉 settings 里可能残留的 tg_* 键，保证每个用例从"没配"开始
        try:
            settings.set_many({
                "tg_token": "", "tg_allowed_users": "", "tg_allowed_chats": "",
            })
        except Exception:
            pass
        self._install(FakeBotModule())

    def tearDown(self) -> None:
        from core import config
        config.TG_TOKEN = self._saved_cfg_token
        tgm._BOT_MODULE = self._saved_token
        try:
            settings.set_many({
                "tg_token": "", "tg_allowed_users": "", "tg_allowed_chats": "",
            })
        except Exception:
            pass

    def _install(self, mod) -> FakeBotModule:
        tgm._BOT_MODULE = mod
        return mod

    def _manager(self, **kw) -> TelegramManager:
        return TelegramManager(**kw)

    # ── 1. import 期零副作用 ─────────────────────────────

    def test_import_has_no_side_effects(self):
        """子进程里只 import core.tgmanager：必须成功、静默、且不加载 telegram 重件。

        判据：
          * 退出码 0
          * stdout 只有我们打的标记（没有日志/警告）
          * `tkinter`-式副作用不存在：telegram 未被 import（本模块不碰它）
          * sys.modules 里没有 requests/httpx 被拉起的痕迹很难判，
            改用更硬的判据：模块导入后 `_BOT_MODULE` 仍是 None（没碰 bot.py）。
        """
        code = (
            "import sys, os\n"
            f"sys.path.insert(0, r'{ROOT}')\n"
            "os.environ['TG_TOKEN']=''\n"
            "os.environ['DATA_DIR']=r'" + str(_TMP) + "'\n"
            "import core.tgmanager as t\n"
            "assert 'telegram' not in sys.modules, '导入期不该加载 telegram'\n"
            "assert 'bot' not in sys.modules, '导入期不该加载 bot.py'\n"
            "assert t._BOT_MODULE is None, '导入期不该碰 bot 模块'\n"
            "print('IMPORT_CLEAN')\n"
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           cwd=str(ROOT))
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr[-800:]}")
        self.assertIn("IMPORT_CLEAN", r.stdout)
        self.assertEqual(r.stdout.strip(), "IMPORT_CLEAN",
                         f"导入期有额外输出（疑似副作用）：{r.stdout!r}")

    def test_no_credentials_dir_created_at_import(self):
        """导入本模块不得创建 data/ 目录（零副作用：不建目录、不初始化 DB）。"""
        fresh = Path(tempfile.mkdtemp(prefix="twitbot-tgm-import-"))
        code = (
            "import sys, os\n"
            f"sys.path.insert(0, r'{ROOT}')\n"
            "os.environ['TG_TOKEN']=''\n"
            f"os.environ['DATA_DIR']=r'{fresh}'\n"
            "import core.tgmanager\n"
            "print('OK', os.listdir(r'" + str(fresh) + "'))\n"
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           cwd=str(ROOT))
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        self.assertIn("OK []", r.stdout, f"导入期创建了文件：{r.stdout!r}")

    # ── 2. status() 字段与脱敏 ───────────────────────────

    def test_status_fields_complete_when_idle(self):
        st = self._manager().status()
        for key in ("running", "token_set", "token_masked", "token_source",
                    "allowed_users", "allowed_chats", "bot_username", "bot_id",
                    "started_at", "last_error", "last_update_at", "updates"):
            self.assertIn(key, st, f"status() 缺字段 {key}")
        self.assertIs(st["running"], False)
        self.assertIs(st["token_set"], False)
        self.assertEqual(st["token_masked"], "")
        self.assertEqual(st["token_source"], "")
        self.assertEqual(st["bot_username"], "")
        self.assertEqual(st["bot_id"], 0)
        self.assertEqual(st["started_at"], 0.0)
        self.assertEqual(st["last_error"], "")
        self.assertEqual(st["last_update_at"], 0.0)
        self.assertEqual(st["updates"], 0)

    def test_status_token_source_settings(self):
        settings.set_many({"tg_token": GOOD_TOKEN})
        st = self._manager().status()
        self.assertIs(st["token_set"], True)
        self.assertEqual(st["token_source"], "settings")
        self.assertNotEqual(st["token_masked"], "")

    def test_status_token_source_env_fallback(self):
        from core import config
        config.TG_TOKEN = GOOD_TOKEN
        st = self._manager().status()
        self.assertEqual(st["token_source"], "env")
        self.assertIs(st["token_set"], True)

    def test_status_never_leaks_full_token(self):
        """核心断言：status() 的任何字段都不得包含完整 token。"""
        settings.set_many({"tg_token": GOOD_TOKEN})
        mgr = self._manager()
        run(mgr.start())                       # 跑起来，让 last_error 等字段也有值
        st = mgr.status()
        blob = json.dumps(st, ensure_ascii=False)
        self.assertNotIn(GOOD_TOKEN, blob)
        # token 冒号后的密钥段也不能出现
        secret = GOOD_TOKEN.split(":", 1)[1]
        self.assertNotIn(secret, blob)
        self.assertIn("...", st["token_masked"])
        self.assertNotEqual(st["token_masked"], GOOD_TOKEN)

    def test_status_masked_shape(self):
        settings.set_many({"tg_token": GOOD_TOKEN})
        masked = self._manager().status()["token_masked"]
        head, _, sec = GOOD_TOKEN.partition(":")
        self.assertTrue(masked.startswith(head[:4]), masked)
        self.assertIn("***", masked)
        self.assertTrue(masked.endswith(sec[-3:]), masked)
        self.assertLess(len(masked), len(GOOD_TOKEN))

    def test_mask_token_edge_cases(self):
        self.assertEqual(mask_token(""), "")
        self.assertEqual(mask_token("   "), "")
        self.assertEqual(mask_token("short"), "***")
        # 掩码结果永远不等于原文
        for t in (GOOD_TOKEN, "abc:def", "x" * 40):
            self.assertNotEqual(mask_token(t), t, f"脱敏后仍是原文：{t!r}")

    # ── 3. start() 幂等 ─────────────────────────────────

    def test_start_is_idempotent(self):
        mod = self._install(FakeBotModule())
        settings.set_many({"tg_token": GOOD_TOKEN})
        mgr = self._manager()

        ok, msg = run(mgr.start())
        self.assertIs(ok, True, msg)
        self.assertEqual(len(mod.apps), 1)
        self.assertEqual(mod.apps[0].updater.polling_starts, 1)

        ok2, msg2 = run(mgr.start())
        self.assertIs(ok2, True)
        self.assertEqual(msg2, "机器人已在运行")
        # 没有第二次 build / 第二次 start_polling
        self.assertEqual(len(mod.apps), 1, "重复 start 又新建了 Application")
        self.assertEqual(mod.apps[0].updater.polling_starts, 1, "重复 start 又起了轮询")

        ok3, msg3 = run(mgr.start())
        self.assertEqual((ok3, msg3), (True, "机器人已在运行"))

    def test_start_reports_bot_identity_and_running(self):
        settings.set_many({"tg_token": GOOD_TOKEN})
        mgr = self._manager()
        ok, msg = run(mgr.start())
        self.assertIs(ok, True, msg)
        self.assertIn("my_test_bot", msg)
        st = mgr.status()
        self.assertIs(st["running"], True)
        self.assertEqual(st["bot_username"], "my_test_bot")
        self.assertEqual(st["bot_id"], 4242)
        self.assertGreater(st["started_at"], 0.0)

    def test_start_persists_explicit_token_and_whitelist(self):
        settings.set_many({"tg_token": ""})
        mgr = self._manager()
        ok, _ = run(mgr.start(token=GOOD_TOKEN, allowed_users="111,222",
                              allowed_chats="-1001"))
        self.assertIs(ok, True)
        self.assertEqual(settings.get("tg_token"), GOOD_TOKEN)
        self.assertEqual(settings.get("tg_allowed_users"), "111,222")
        self.assertEqual(settings.get("tg_allowed_chats"), "-1001")

    def test_start_does_not_persist_invalid_token(self):
        """无效 token 不该被写进 settings（控制台别存垃圾）。"""
        settings.set_many({"tg_token": ""})
        mod = FakeBotModule(app_factory=lambda token: FakeApplication(
            token=token, fail_at="token"))
        self._install(mod)
        mgr = self._manager()
        ok, msg = run(mgr.start(token=BAD_TOKEN))
        self.assertIs(ok, False)
        self.assertEqual(settings.get("tg_token"), "", "无效 token 被写进了 settings")
        self.assertIn("无效", msg)

    def test_start_while_running_applies_new_whitelist(self):
        """已在运行时传入的新白名单仍应生效（运行期现读，无需重启）。"""
        settings.set_many({"tg_token": GOOD_TOKEN, "tg_allowed_users": "1"})
        mgr = self._manager()
        run(mgr.start())
        ok, msg = run(mgr.start(allowed_users="7,8"))
        self.assertIs(ok, True)
        self.assertEqual(msg, "机器人已在运行")
        self.assertEqual(settings.get("tg_allowed_users"), "7,8")

    # ── 4. 失败路径绝不抛异常 ────────────────────────────

    def test_start_without_token_returns_chinese_reason(self):
        mgr = self._manager()
        ok, msg = run(mgr.start())
        self.assertIs(ok, False)
        self.assertTrue(msg, "必须给出可读原因")
        self.assertIn("token", msg.lower())
        self.assertIs(mgr.status()["running"], False)

    def test_start_invalid_token_says_botfather(self):
        """token 无效（401）必须给人话，且指出去哪重新拿。"""
        self._install(FakeBotModule(app_factory=lambda token: FakeApplication(
            token=token, fail_at="token")))
        mgr = self._manager()
        ok, msg = run(mgr.start(token=BAD_TOKEN))
        self.assertIs(ok, False)
        self.assertIn("BotFather", msg)
        self.assertIn("无效", msg)
        self.assertIs(mgr.status()["running"], False)

    def test_invalid_token_error_message_is_scrubbed(self):
        """PTB 的 InvalidToken 消息里带 token —— last_error 与返回值都必须脱敏。"""
        self._install(FakeBotModule(app_factory=lambda token: FakeApplication(
            token=token, fail_at="token")))
        mgr = self._manager()
        ok, msg = run(mgr.start(token=BAD_TOKEN))
        self.assertIs(ok, False)
        secret = BAD_TOKEN.split(":", 1)[1]
        self.assertNotIn(BAD_TOKEN, msg)
        self.assertNotIn(secret, msg)
        err = mgr.status()["last_error"]
        self.assertNotIn(BAD_TOKEN, err)
        self.assertNotIn(secret, err)
        # 脱敏后再落库/落状态，整份 status 也不该漏
        self.assertNotIn(secret, json.dumps(mgr.status(), ensure_ascii=False))

    def test_start_network_error_is_friendly(self):
        self._install(FakeBotModule(app_factory=lambda token: FakeApplication(
            token=token, fail_at="init", fail_with=FakeNetworkError("cannot connect"))))
        mgr = self._manager()
        ok, msg = run(mgr.start(token=GOOD_TOKEN))
        self.assertIs(ok, False)
        self.assertIn("网络", msg)

    def test_start_conflict_is_friendly(self):
        self._install(FakeBotModule(app_factory=lambda token: FakeApplication(
            token=token, fail_at="conflict")))
        mgr = self._manager()
        ok, msg = run(mgr.start(token=GOOD_TOKEN))
        self.assertIs(ok, False)
        self.assertIn("轮询", msg)

    def test_start_failure_midway_cleans_up(self):
        """initialize 成功但 start_polling 失败 → 必须收尾，不留半开状态。"""
        mod = self._install(FakeBotModule(app_factory=lambda token: FakeApplication(
            token=token, fail_at="polling")))
        mgr = self._manager()
        ok, msg = run(mgr.start(token=GOOD_TOKEN))
        self.assertIs(ok, False)
        app = mod.apps[0]
        self.assertGreaterEqual(app.shutdowns, 1, "失败后没有 shutdown")
        self.assertIs(app.updater.running, False)
        self.assertIs(mgr.status()["running"], False)

    def test_start_can_retry_after_failure(self):
        """失败后仍能重新 start（状态没被卡死）。"""
        mod = self._install(FakeBotModule(app_factory=lambda token: FakeApplication(
            token=token, fail_at="polling")))
        mgr = self._manager()
        ok1, _ = run(mgr.start(token=GOOD_TOKEN))
        self.assertIs(ok1, False)

        mod2 = self._install(FakeBotModule())
        ok2, msg2 = run(mgr.start(token=GOOD_TOKEN))
        self.assertIs(ok2, True, msg2)

    def test_start_never_raises_on_exotic_failure(self):
        """build_application 抛任意异常都不能穿透（Web 会 500）。"""
        class BoomModule:
            def build_application(self, token=None):
                raise ValueError("something exploded with token abc:defghijklmnopqrst")

        self._install(BoomModule())
        mgr = self._manager()
        ok, msg = run(mgr.start(token=GOOD_TOKEN))
        self.assertIs(ok, False)
        self.assertTrue(msg)

    def test_start_missing_dependency_is_graceful(self):
        """缺 python-telegram-bot → 中文提示，不崩、不抛。"""
        def _boom():
            raise tgm.TgDependencyError(tgm.MSG_NO_DEPS)

        self._install(None)
        original = tgm._load_bot_module
        tgm._load_bot_module = _boom
        try:
            mgr = self._manager()
            ok, msg = run(mgr.start(token=GOOD_TOKEN))
            self.assertIs(ok, False)
            self.assertIn("python-telegram-bot", msg)
            self.assertIs(mgr.status()["running"], False)
        finally:
            tgm._load_bot_module = original

    # ── 5. stop() / restart() ───────────────────────────

    def test_stop_when_not_running(self):
        mgr = self._manager()
        ok, msg = run(mgr.stop())
        self.assertIs(ok, True)
        self.assertEqual(msg, "机器人未在运行")

    def test_stop_frees_polling(self):
        mod = self._install(FakeBotModule())
        mgr = self._manager()
        run(mgr.start(token=GOOD_TOKEN))
        app = mod.apps[0]
        self.assertIs(app.updater.running, True)

        ok, msg = run(mgr.stop())
        self.assertIs(ok, True, msg)
        self.assertIs(app.updater.running, False)
        self.assertGreaterEqual(app.shutdowns, 1)
        st = mgr.status()
        self.assertIs(st["running"], False)
        self.assertEqual(st["started_at"], 0.0)

        # 停了还能再起
        ok2, _ = run(mgr.start(token=GOOD_TOKEN))
        self.assertIs(ok2, True)
        self.assertEqual(len(mod.apps), 2)

    def test_stop_tolerates_partial_failures(self):
        """updater.stop()/shutdown() 抛异常也要返回 (True, ...) 并注明告警。"""
        for where in ("updater_stop", "app_shutdown"):
            with self.subTest(where=where):
                mod = self._install(FakeBotModule(app_factory=lambda token, w=where:
                                                  FakeApplication(token=token, fail_at=w)))
                mgr = self._manager()
                run(mgr.start(token=GOOD_TOKEN))
                ok, msg = run(mgr.stop())
                self.assertIs(ok, True, msg)
                self.assertIs(mgr.status()["running"], False)
                self.assertEqual(len(mod.apps), 1)

    def test_restart_roundtrip(self):
        mod = self._install(FakeBotModule())
        mgr = self._manager()
        ok, msg = run(mgr.restart(token=GOOD_TOKEN))
        self.assertIs(ok, True, msg)
        self.assertEqual(len(mod.apps), 1)
        self.assertIs(mgr.status()["running"], True)

        ok2, msg2 = run(mgr.restart(token=GOOD_TOKEN))
        self.assertIs(ok2, True, msg2)
        self.assertEqual(len(mod.apps), 2, "restart 没有重新构造 Application")

    def test_restart_failure_is_reported_not_raised(self):
        self._install(FakeBotModule(app_factory=lambda token: FakeApplication(
            token=token, fail_at="token")))
        mgr = self._manager()
        ok, msg = run(mgr.restart(token=BAD_TOKEN))
        self.assertIs(ok, False)
        self.assertIn("失败", msg)
        self.assertIs(mgr.status()["running"], False)

    # ── 6. verify_token() ───────────────────────────────

    def test_verify_token_success_does_not_start_polling(self):
        mod = self._install(FakeBotModule())
        mgr = self._manager()
        ok, msg, info = run(mgr.verify_token(GOOD_TOKEN))
        self.assertIs(ok, True, msg)
        self.assertEqual(info["bot_username"], "my_test_bot")
        self.assertEqual(info["bot_id"], 4242)
        # 只校验：不启动轮询、状态仍是未运行
        self.assertEqual(mod.apps[0].updater.polling_starts, 0)
        self.assertIs(mgr.status()["running"], False)
        # 校验用的 Application 已收尾
        self.assertGreaterEqual(mod.apps[0].shutdowns, 1)

    def test_verify_token_invalid(self):
        self._install(FakeBotModule(app_factory=lambda token: FakeApplication(
            token=token, fail_at="token")))
        mgr = self._manager()
        ok, msg, info = run(mgr.verify_token(BAD_TOKEN))
        self.assertIs(ok, False)
        self.assertIn("BotFather", msg)
        self.assertEqual(info.get("bot_username", ""), "")
        self.assertNotIn(BAD_TOKEN, msg)

    def test_verify_token_empty_and_malformed(self):
        mgr = self._manager()
        ok, msg, info = run(mgr.verify_token(""))
        self.assertIs(ok, False)
        self.assertTrue(msg)
        self.assertIsInstance(info, dict)

        ok2, msg2, _ = run(mgr.verify_token("not-a-token"))
        self.assertIs(ok2, False)
        self.assertTrue(msg2)

    def test_verify_token_never_raises(self):
        class BoomModule:
            def build_application(self, token=None):
                raise RuntimeError("boom")

        self._install(BoomModule())
        mgr = self._manager()
        ok, msg, info = run(mgr.verify_token(GOOD_TOKEN))
        self.assertIs(ok, False)
        self.assertIsInstance(info, dict)
        self.assertTrue(msg)

    # ── 7. 白名单 / 状态细节 ─────────────────────────────

    def test_status_reports_settings_whitelist(self):
        settings.set_many({"tg_allowed_users": "11,22", "tg_allowed_chats": "-100"})
        st = self._manager().status()
        self.assertEqual(st["allowed_users"], "11,22")
        self.assertEqual(st["allowed_chats"], "-100")

    def test_status_falls_back_to_config_whitelist(self):
        """settings 为空时，status 显示 config（.env）里实际生效的白名单。"""
        from core import config
        saved_u, saved_c = config.ALLOWED_USERS, config.ALLOWED_CHATS
        config.ALLOWED_USERS = {5, 3}
        config.ALLOWED_CHATS = {-100}
        try:
            st = self._manager().status()
            self.assertEqual(st["allowed_users"], "3,5")
            self.assertEqual(st["allowed_chats"], "-100")
        finally:
            config.ALLOWED_USERS, config.ALLOWED_CHATS = saved_u, saved_c

    def test_status_updates_counter_comes_from_bot_module(self):
        settings.set_many({"tg_token": GOOD_TOKEN})
        mgr = self._manager()
        run(mgr.start())
        first = mgr.status()["updates"]
        second = mgr.status()["updates"]
        self.assertGreater(second, first, "updates 应随 bot 模块计数递增")
        self.assertGreater(mgr.status()["last_update_at"], 0.0)

    def test_on_log_callback_is_invoked_and_safe(self):
        seen: list[str] = []
        mgr = TelegramManager(on_log=seen.append)
        run(mgr.start(token=GOOD_TOKEN))
        self.assertTrue(seen, "on_log 没有被调用")

        # 回调自身抛异常不得影响主流程
        def bad(_msg):
            raise RuntimeError("callback boom")
        mgr2 = TelegramManager(on_log=bad)
        ok, msg = run(mgr2.start(token=GOOD_TOKEN))
        self.assertIs(ok, True, msg)

    def test_no_token_in_any_log_record(self):
        """日志里也不许出现 token（AGENTS.md：secrets 绝不落日志）。

        用 logging handler 抓本模块全部日志，跑一遍成功 + 失败路径。
        """
        import logging

        records: list[str] = []

        class Grab(logging.Handler):
            def emit(self, record):
                try:
                    # getMessage() 已经做过 %-格式化，不要再套一次 %
                    records.append(record.getMessage())
                except Exception:
                    records.append(str(record.msg))

        handler = Grab(level=logging.DEBUG)
        logger = logging.getLogger("twitbot.tgmanager")
        old_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            settings.set_many({"tg_token": ""})
            # 失败路径（InvalidToken 消息里带 token）
            self._install(FakeBotModule(app_factory=lambda token: FakeApplication(
                token=token, fail_at="token")))
            mgr = TelegramManager()
            run(mgr.start(token=BAD_TOKEN))
            mgr.status()
            run(mgr.verify_token(BAD_TOKEN))
            # 成功路径
            self._install(FakeBotModule())
            ok, _ = run(mgr.start(token=GOOD_TOKEN))
            self.assertIs(ok, True)
            run(mgr.stop())
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)

        blob = "\n".join(records)
        self.assertTrue(records, "没抓到任何日志，测试本身失效了")
        for tok in (GOOD_TOKEN, BAD_TOKEN):
            self.assertNotIn(tok, blob, "日志里出现了完整 token")
            self.assertNotIn(tok.split(":", 1)[1], blob,
                             "日志里出现了 token 密钥段")

    def test_manager_is_reusable_across_event_loops(self):
        """同一个实例跨多次 asyncio.run（不同事件循环）不得报 loop 绑定错误。

        这是 Web 控制台的真实调用形态：起/停分属不同请求、不同事件循环。
        且第二轮起必须能重新拉起来（不是"已在运行"）。
        """
        settings.set_many({"tg_token": GOOD_TOKEN})
        mgr = self._manager()
        for i in range(3):
            ok, msg = run(mgr.start())
            self.assertIs(ok, True, f"第 {i + 1} 轮启动失败：{msg}")
            self.assertNotEqual(msg, "机器人已在运行",
                                f"第 {i + 1} 轮被误判为已在运行（stop 没清干净）")
            self.assertIs(mgr.status()["running"], True)
            ok2, msg2 = run(mgr.stop())
            self.assertIs(ok2, True, msg2)
        self.assertIs(mgr.status()["running"], False)

    def test_public_methods_never_raise_on_broken_settings(self):
        """settings 层炸了也不能让 status/start 抛（Web 会 500）。"""
        original = tgm._settings

        def _boom():
            raise RuntimeError("settings exploded")

        tgm._settings = _boom
        try:
            mgr = self._manager()
            st = mgr.status()          # 不抛
            self.assertIsInstance(st, dict)
            ok, msg = run(mgr.start(token=GOOD_TOKEN))   # 不抛，用显式 token 仍能起
            self.assertIs(ok, True, msg)
            run(mgr.stop())
        finally:
            tgm._settings = original


# ══════════════════════════════════════════════════════════
# bot.py 侧改动的单元测试（§2.3）
# ══════════════════════════════════════════════════════════

class BotModuleWiringTest(unittest.TestCase):
    """验证 bot.py 的 §2.3 改动：token 取值顺序、白名单回落、计数、导出名不丢。"""

    def setUp(self) -> None:
        import telegram

        from core import config
        self._telegram = telegram
        self._cfg_token = config.TG_TOKEN
        config.TG_TOKEN = ""
        settings.set_many({
            "tg_token": "", "tg_allowed_users": "", "tg_allowed_chats": "",
        })
        self._reset_admins()
        self.bot = self._import_bot()

    @staticmethod
    def _reset_admins() -> None:
        """2026-09-26 起「白名单」的真身是 tg_admins 列表，不清它用例之间会串台
        （settings 里那串数字会被迁移进列表，然后留在库里）。"""
        try:
            from core import queue
            with queue.db() as con:
                for t in ("tg_admins", "tg_signups"):
                    try:
                        con.execute(f"DELETE FROM {t}")
                    except Exception:
                        pass
        except Exception:
            pass

    def tearDown(self) -> None:
        from core import config
        config.TG_TOKEN = self._cfg_token
        settings.set_many({
            "tg_token": "", "tg_allowed_users": "", "tg_allowed_chats": "",
        })
        self._reset_admins()

    @staticmethod
    def _import_bot():
        import importlib.util
        spec = importlib.util.spec_from_file_location("_bot_under_test", ROOT / "bot.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    # ── 导出名向后兼容（smoke.py 依赖）─────────────────

    def test_all_legacy_exports_still_exist(self):
        for name in ("load_dotenv", "init_db", "enqueue", "claim_next", "mark",
                     "month_sent_count", "compose", "weighted_len", "first_tweet_id",
                     "single_instance_lock", "db", "now_iso", "build_application",
                     "is_dup_content", "queue_stats", "content_hash", "resolve_media",
                     "MODE", "DRY_RUN", "TG_TOKEN", "ALLOWED_USERS", "ALLOWED_CHATS",
                     "on_message", "on_callback", "cmd_start", "cmd_status",
                     "cmd_queue", "cmd_retry", "post_init", "post_shutdown",
                     "raise_keyboard_interrupt", "get_pipeline", "main"):
            self.assertTrue(hasattr(self.bot, name), f"bot.py 丢了既有导出名：{name}")

    def test_authorized_helpers_exposed(self):
        self.assertTrue(callable(self.bot.allowed_users_effective))
        self.assertTrue(callable(self.bot.allowed_chats_effective))
        self.assertTrue(callable(self.bot.updates_seen))
        self.assertTrue(callable(self.bot.last_update_at))
        self.assertTrue(callable(self.bot.reset_update_stats))

    # ── 白名单：settings 优先，空则回落 config ───────────

    def test_whitelist_prefers_settings(self):
        """老字段 settings.tg_allowed_users 会被**迁移**成管理员列表。

        2026-09-26 起白名单的真身是 tg_admins 列表（有名字/加入时间/审批人），
        settings 里那串数字只在列表为空时提供种子。
        """
        from core import config
        saved = config.ALLOWED_USERS
        config.ALLOWED_USERS = {1, 2, 3}
        try:
            # 列表空 → 回落 config
            self.assertEqual(self.bot.allowed_users_effective(), {1, 2, 3})
            # 老字段有值 → 迁移进列表，以列表为准
            settings.set_many({"tg_allowed_users": "9, 10"})
            self.assertEqual(self.bot.allowed_users_effective(), {9, 10})
            # 迁移后列表说了算：清空老字段不再影响生效值（这正是列表化的目的）
            settings.set_many({"tg_allowed_users": ""})
            self.assertEqual(self.bot.allowed_users_effective(), {9, 10})
            # 把列表清掉才回到 config
            self._reset_admins()
            self.assertEqual(self.bot.allowed_users_effective(), {1, 2, 3})
        finally:
            config.ALLOWED_USERS = saved

    def test_whitelist_chats_prefers_settings(self):
        from core import config
        saved = config.ALLOWED_CHATS
        config.ALLOWED_CHATS = {-100}
        try:
            settings.set_many({"tg_allowed_chats": "-200,-300"})
            self.assertEqual(self.bot.allowed_chats_effective(), {-200, -300})
            settings.set_many({"tg_allowed_chats": ""})
            self.assertEqual(self.bot.allowed_chats_effective(), {-100})
        finally:
            config.ALLOWED_CHATS = saved

    def test_whitelist_ignores_garbage(self):
        """只认**正数** user id —— 字母/空项/@xxx/负数都不是 Telegram 用户。

        （负数在 Telegram 里代表群/频道，那是 tg_allowed_chats 的范畴；
         管理员永远是人，所以负数会被丢掉。）
        """
        settings.set_many({"tg_allowed_users": "abc, 12, ,@x, -3"})
        self.assertEqual(self.bot.allowed_users_effective(), {12})

    # ── _authorized 行为保留 ────────────────────────────

    def _fake_update(self, chat_id=1, user_id=1):
        class Chat:
            id = chat_id
        class User:
            id = user_id
        class Msg:
            from_user = User()
        class Upd:
            effective_chat = Chat()
            effective_message = Msg()
        return Upd()

    def test_authorized_blocks_non_whitelisted_chat(self):
        from core import config
        saved = config.ALLOWED_CHATS
        config.ALLOWED_CHATS = {999}
        try:
            ok, why = asyncio.run(self.bot._authorized(self._fake_update(chat_id=1)))
            self.assertIs(ok, False)
            self.assertIn("ALLOWED_CHATS", why)
        finally:
            config.ALLOWED_CHATS = saved

    def test_authorized_blocks_non_whitelisted_user(self):
        from core import config
        saved_u, saved_c = config.ALLOWED_USERS, config.ALLOWED_CHATS
        config.ALLOWED_CHATS = set()
        config.ALLOWED_USERS = {42}
        try:
            ok, why = asyncio.run(self.bot._authorized(self._fake_update(user_id=7)))
            self.assertIs(ok, False)
            self.assertIn("管理员列表", why)
            ok2, _ = asyncio.run(self.bot._authorized(self._fake_update(user_id=42)))
            self.assertIs(ok2, True)
        finally:
            config.ALLOWED_USERS, config.ALLOWED_CHATS = saved_u, saved_c

    def test_authorized_allows_when_both_empty(self):
        from core import config
        saved_u, saved_c = config.ALLOWED_USERS, config.ALLOWED_CHATS
        config.ALLOWED_CHATS = set()
        config.ALLOWED_USERS = set()
        try:
            ok, why = asyncio.run(self.bot._authorized(self._fake_update()))
            self.assertIs(ok, True)
            self.assertEqual(why, "")
        finally:
            config.ALLOWED_USERS, config.ALLOWED_CHATS = saved_u, saved_c

    def test_authorized_settings_whitelist_is_enforced(self):
        """settings 里的老白名单会被迁移成管理员列表 → 立刻生效（无需重启进程）。"""
        settings.set_many({"tg_allowed_users": "555"})
        ok, why = asyncio.run(self.bot._authorized(self._fake_update(user_id=1)))
        self.assertIs(ok, False)
        self.assertIn("管理员列表", why)
        # 放行白名单内的人
        ok2, why2 = asyncio.run(self.bot._authorized(self._fake_update(user_id=555)))
        self.assertIs(ok2, True)
        self.assertEqual(why2, "")

    def test_authorized_no_chat_or_message(self):
        class Upd:
            effective_chat = None
            effective_message = None
        ok, why = asyncio.run(self.bot._authorized(Upd()))
        self.assertIs(ok, False)

    # ── build_application token 取值顺序 ────────────────

    def test_build_application_token_precedence(self):
        """settings.tg_token 优先于 config.TG_TOKEN；不传参时行为与旧版一致。"""
        from core import config
        config.TG_TOKEN = "111:fromEnvConfig________"
        app = self.bot.build_application()
        self.assertIn("fromEnvConfig", app.bot.token)

        settings.set_many({"tg_token": "222:fromSettings_______"})
        app2 = self.bot.build_application()
        self.assertIn("fromSettings", app2.bot.token)

        # 显式传参最高优先
        app3 = self.bot.build_application("333:explicit___________")
        self.assertIn("explicit", app3.bot.token)

    def test_build_application_raises_clear_systemexit_without_token(self):
        from core import config
        saved = config.TG_TOKEN
        config.TG_TOKEN = ""
        try:
            with self.assertRaises(SystemExit) as ctx:
                self.bot.build_application()
            self.assertIn("TG_TOKEN", str(ctx.exception))
        finally:
            config.TG_TOKEN = saved

    def test_build_application_registers_all_handlers(self):
        """复用既有注册：命令/回调/消息/错误 + 统计 handler 都在。"""
        settings.set_many({"tg_token": GOOD_TOKEN})
        app = self.bot.build_application()
        groups = sorted(app.handlers)
        self.assertIn(0, groups, "业务 handler 不在 group 0")
        self.assertIn(-1, groups, "统计 handler 应在 group -1")
        total = sum(len(v) for v in app.handlers.values())
        self.assertGreaterEqual(total, 8, f"handler 数量异常：{total}")
        self.assertTrue(app.error_handlers, "错误 handler 丢了")
        # post_init / post_shutdown 仍挂在 Application 上
        self.assertIsNotNone(getattr(app, "post_init", None))
        self.assertIsNotNone(getattr(app, "post_shutdown", None))

    def test_update_count_type_is_just_update(self):
        """统计 handler 只需要 telegram.Update 一个。

        `Update.ALL_TYPES` 里的 "message" 等是 Update 的*字段*，对应的
        `Message` 类**不是** Update 的子类 —— 拿它注册 TypeHandler 永不命中，
        纯属浪费（13 个 handler 里 12 个是死代码）。
        """
        types = self.bot._update_count_types()
        self.assertEqual(len(types), 1, f"统计类型应为 1 个，实际 {len(types)}")
        self.assertIs(types[0], self.bot.Update)
        # 幂等缓存
        self.assertIs(self.bot._update_count_types(), types)

    def test_update_counter_increments(self):
        self.bot.reset_update_stats()
        self.assertEqual(self.bot.updates_seen(), 0)
        self.assertEqual(self.bot.last_update_at(), 0.0)
        asyncio.run(self.bot.on_any_update(object(), None))
        self.assertEqual(self.bot.updates_seen(), 1)
        self.assertGreater(self.bot.last_update_at(), 0.0)
        asyncio.run(self.bot.on_any_update(object(), None))
        self.assertEqual(self.bot.updates_seen(), 2)

    def test_update_counter_counts_once_per_update_via_real_dispatch(self):
        """走真实 `Application.process_update`：一个 update 只记一次。

        （回归：group -1 里挂多个 TypeHandler 时，PTB 每个 group 只跑第一个命中的
        handler，但仍要确认计数不会被重复累加，也不会因为 handler 抛异常而漏记。）
        """
        settings.set_many({"tg_token": GOOD_TOKEN})
        telegram = self._telegram

        async def fake_do_post(_self, endpoint, *a, **kw):
            ep = str(endpoint)
            if "getMe" in ep:
                return {"id": 999, "is_bot": True,
                        "first_name": "b", "username": "check_bot"}
            if "getUpdates" in ep:
                return []
            return True

        saved = telegram.Bot._do_post
        telegram.Bot._do_post = fake_do_post
        try:
            async def scenario():
                app = self.bot.build_application()
                await app.initialize()
                self.assertEqual(len(app.handlers.get(-1, [])), 1,
                                 "group -1 里不该有多个统计 handler")
                upd = telegram.Update(
                    update_id=1,
                    message=telegram.Message(
                        message_id=1, date=None,
                        chat=telegram.Chat(id=1, type="private"),
                        from_user=telegram.User(id=1, first_name="t",
                                                is_bot=False),
                        text="hi"))
                self.bot.reset_update_stats()
                await app.process_update(upd)
                self.assertEqual(self.bot.updates_seen(), 1)
                await app.process_update(upd)
                self.assertEqual(self.bot.updates_seen(), 2)
                await app.shutdown()

            asyncio.run(scenario())
        finally:
            telegram.Bot._do_post = saved
            self.bot.reset_update_stats()

    def test_update_counter_types_resolvable(self):
        types = self.bot._update_count_types()
        self.assertTrue(types, "没能解析出任何 update 类型，统计 handler 会形同虚设")
        for t in types:
            self.assertIsInstance(t, type)

    def test_legacy_exports_are_the_real_core_functions(self):
        """导出名不只是"存在"，还必须与 core 里的实现是同一个对象。

        注意：不断言 month_sent_count() 的数值 —— pytest 全量跑时各测试模块共用
        同一个 DATA_DIR 数据库，队列里可能已有别的用例写进去的 sent 行。
        """
        from core import queue, textutil
        from core.lock import single_instance_lock as _lock
        self.assertIs(self.bot.init_db, queue.init_db)
        self.assertIs(self.bot.enqueue, queue.enqueue)
        self.assertIs(self.bot.claim_next, queue.claim_next)
        self.assertIs(self.bot.mark, queue.mark)
        self.assertIs(self.bot.month_sent_count, queue.month_sent_count)
        self.assertIs(self.bot.db, queue.db)
        self.assertIs(self.bot.now_iso, queue.now_iso)
        self.assertIs(self.bot.compose, textutil.compose)
        self.assertIs(self.bot.weighted_len, textutil.weighted_len)
        self.assertIs(self.bot.first_tweet_id, textutil.first_tweet_id)
        self.assertIs(self.bot.single_instance_lock, _lock)
        self.assertIsInstance(self.bot.MAX_ATTEMPTS, int)
        self.assertIsInstance(self.bot.month_sent_count(), int)


# ══════════════════════════════════════════════════════════
# 真 PTB 集成：用真实 Application/Updater 跑完整生命周期
# ══════════════════════════════════════════════════════════
#
# 上面用的是假 Application（测本模块逻辑）。这一组换成**真实的**
# `telegram.ext.Application`，只把 HTTP 层换成桩（绝不联网），
# 目的是验证：
#   * build_application 真的能构造出 PTB 能吃的 Application
#   * 真 Updater 的 stop 路径不会因为 `updater.running`/`app.running`
#     时序问题而留下半开状态或抛异常
#   * 反复 start/stop 不泄漏、不卡死

class RealPtbLifecycleTest(unittest.TestCase):
    """真 PTB + 桩 HTTP。离线、不联网。"""

    def setUp(self) -> None:
        import telegram
        from telegram.ext import Application  # noqa: F401

        self._telegram = telegram
        self._saved_do_post = telegram.Bot._do_post
        self._saved_module = getattr(tgm, "_BOT_MODULE", None)

        async def fake_do_post(_self, endpoint, *a, **kw):
            ep = str(endpoint)
            if "getMe" in ep:
                return {"id": 7712345, "is_bot": True,
                        "first_name": "bot", "username": "lifecycle_bot"}
            if "getUpdates" in ep:
                return []          # 必须返回 list，PTB 收尾会再拉一次
            return True

        telegram.Bot._do_post = fake_do_post

        class RealAppModule:
            @staticmethod
            def build_application(token=None):
                from telegram.ext import Application
                return Application.builder().token(token).concurrent_updates(True).build()

            @staticmethod
            def reset_update_stats():
                pass

        tgm._BOT_MODULE = RealAppModule()

    def tearDown(self) -> None:
        self._telegram.Bot._do_post = self._saved_do_post
        tgm._BOT_MODULE = self._saved_module
        try:
            settings.set_many({"tg_token": "", "tg_allowed_users": "",
                               "tg_allowed_chats": ""})
        except Exception:
            pass

    def test_full_lifecycle_with_real_ptb(self):
        """整个生命周期跑在**同一个事件循环**里。

        真 PTB 的 HTTP 客户端绑定在创建它的 loop 上，跨 `asyncio.run` 会炸
        （`Event loop is closed`），所以这里不能像上面那组假 Application 那样
        每次调用单独 `asyncio.run`。—— 这也正是 Web 控制台要留意的约束：
        同一进程内 start/stop 应复用同一个事件循环。
        """
        async def scenario():
            mgr = TelegramManager()
            tok = "123456789:AAHlifecycle_integration_x"

            ok, msg = await mgr.start(token=tok)
            self.assertIs(ok, True, msg)
            self.assertIn("lifecycle_bot", msg)

            st = mgr.status()
            self.assertIs(st["running"], True)
            self.assertEqual(st["bot_id"], 7712345)
            self.assertEqual(st["bot_username"], "lifecycle_bot")
            self.assertGreater(st["started_at"], 0.0)

            # 幂等：不重复起轮询
            self.assertEqual(await mgr.start(), (True, "机器人已在运行"))

            # 收尾必须干净：无告警、running 归位、started_at 清零
            ok2, msg2 = await mgr.stop()
            self.assertIs(ok2, True, msg2)
            self.assertEqual(msg2, "机器人已停止", f"收尾有告警：{msg2}")
            self.assertIs(mgr.status()["running"], False)
            self.assertEqual(mgr.status()["started_at"], 0.0)

            # 停了能再起（真 Updater 没有残留状态）
            ok3, msg3 = await mgr.start()
            self.assertIs(ok3, True, msg3)
            self.assertIs(mgr.status()["running"], True)
            self.assertIs((await mgr.stop())[0], True)
            self.assertIs(mgr.status()["running"], False)

        asyncio.run(scenario())

    def test_real_ptb_invalid_token_is_scrubbed(self):
        """真 PTB 的 InvalidToken 会把 token 原样写进消息 —— 必须脱敏。"""
        bad = "123456789:AAHrealptb_invalidtoken_z"
        original = self._telegram.Bot._do_post

        async def scenario():
            mgr = TelegramManager()

            async def fake(_self, endpoint, *a, **kw):
                if "getMe" in str(endpoint):
                    raise self._telegram.error.InvalidToken(
                        f"The token `{bad}` was rejected by the server.")
                return True

            self._telegram.Bot._do_post = fake
            try:
                ok, msg = await mgr.start(token=bad)
            finally:
                self._telegram.Bot._do_post = original

            self.assertIs(ok, False)
            self.assertIn("BotFather", msg)
            self.assertNotIn(bad, msg)
            self.assertNotIn(bad.split(":", 1)[1], msg)
            err = mgr.status()["last_error"]
            self.assertNotIn(bad.split(":", 1)[1], err)
            self.assertIs(mgr.status()["running"], False)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ══════════════════════════════════════════════════════════
# 相册聚合 / 多图入队（本次新增）
#   Telegram 一次选 4 张图 = 4 条消息 + 同一个 media_group_id。
#   必须合并成 **1 条任务**，否则会变成 4 条独立推文。
# ══════════════════════════════════════════════════════════

def _fake_album_msg(mid, cap="", gid="G1", chat_id=555):
    """构造一条"相册消息"的假对象（够 bot._enqueue_album 用）。"""
    import types

    class FakeFile:
        async def download_to_drive(self, custom_path=None):
            import pathlib
            pathlib.Path(custom_path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20)
            return custom_path

    class FakePhoto:
        async def get_file(self):
            return FakeFile()

    m = types.SimpleNamespace()
    m.message_id = mid
    m.chat_id = chat_id
    m.chat = types.SimpleNamespace(id=chat_id)
    m.caption = cap
    m.text = ""
    m.media_group_id = gid
    m.photo = [FakePhoto()]
    m.video = None
    m.document = None
    m.replies = []

    async def _reply(text, **kw):
        m.replies.append((text, kw))
    m.reply_text = _reply
    return m


def test_media_group_id_extraction():
    import bot
    m = _fake_album_msg(1)
    assert bot._media_group_id(m) == "G1"
    m.media_group_id = None
    assert bot._media_group_id(m) == ""


def test_album_merges_four_messages_into_one_job():
    """核心回归：4 条相册消息 -> 恰好 1 条任务，且 4 张图都在。"""
    import asyncio
    import bot
    from core import queue, media as media_mod

    bot._albums.clear()
    queue.init_db()

    msgs = [_fake_album_msg(101, "四图配文"), _fake_album_msg(102),
            _fake_album_msg(103), _fake_album_msg(104)]

    before = len(queue.recent(limit=200))

    async def run():
        await bot._enqueue_album(msgs)
    asyncio.run(run())

    rows = queue.recent(limit=200)
    assert len(rows) == before + 1, f"应只新增 1 条任务，实际新增 {len(rows) - before}"

    row = rows[0]
    paths = media_mod.parse_media_paths(row["media_path"])
    assert len(paths) == 4, f"应有 4 张图，实际 {len(paths)}"
    # 配文来自第一条带 caption 的消息
    assert "四图配文" in (row["raw_text"] or "")


def test_album_over_four_is_trimmed(tmp_path):
    """超过 4 张要截断到 4 张，且在回复里**告知**用户。"""
    import asyncio
    import bot
    from core import queue, media as media_mod

    bot._albums.clear()
    queue.init_db()
    msgs = [_fake_album_msg(200 + i) for i in range(6)]
    before = len(queue.recent(limit=200))

    async def run():
        await bot._enqueue_album(msgs)
    asyncio.run(run())

    rows = queue.recent(limit=200)
    assert len(rows) == before + 1
    paths = media_mod.parse_media_paths(rows[0]["media_path"])
    assert len(paths) == media_mod.MAX_IMAGES == 4


def test_edit_waiting_state_is_per_chat():
    """「改文字」按会话记，取出后即清除（避免下次文本被误当成改文字）。"""
    import bot
    bot._edit_waiting.clear()
    bot._mark_pending_edit(111, 7)
    bot._mark_pending_edit(222, 8)
    assert bot._take_pending_edit(111) == 7
    assert bot._take_pending_edit(111) is None      # 取过就清掉
    assert bot._take_pending_edit(222) == 8


def test_content_hash_includes_media_name(tmp_path):
    """两张不同的图 + 同样文字，不能被内容去重误判为重复。"""
    from core import textutil
    a = textutil.content_hash("photo", "hello", "", "imgA.jpg")
    b = textutil.content_hash("photo", "hello", "", "imgB.jpg")
    assert a != b, "不同图片必须得到不同指纹"
    same = textutil.content_hash("photo", "hello", "", "imgA.jpg")
    assert a == same, "同样的图+文字应得到相同指纹（去重要生效）"


# ══════════════════════════════════════════════════════════
# 评论（回复）：会话状态机
# ══════════════════════════════════════════════════════════

def test_reply_state_is_per_chat():
    """评论流程状态按会话记，且取出后即清除。"""
    import bot
    bot._reply_state.clear()
    bot._mark_reply(111, "text")
    st = bot._peek_reply(111)
    assert st["stage"] == "link" and st["mode"] == "text"
    # 另一个会话不受影响
    assert bot._peek_reply(222) == {}
    bot._set_reply_target(111, "1234567890")
    assert bot._peek_reply(111)["stage"] == "choose"
    assert bot._peek_reply(111)["target"] == "1234567890"
    got = bot._take_reply(111)
    assert got["target"] == "1234567890"
    assert bot._peek_reply(111) == {}, "取出后必须清掉，否则下一条消息会被误当评论"


def test_enqueue_album_keeps_reply_target():
    """图文评论走相册聚合时，回复目标不能被丢掉。"""
    import asyncio
    import bot
    from core import queue, media as media_mod, reply as reply_mod

    bot._albums.clear()
    queue.init_db()
    before = len(queue.recent(limit=200))
    msgs = [_fake_album_msg(201, "图片评论文字"), _fake_album_msg(202)]

    async def run():
        await bot._enqueue_album(msgs, quote_id=reply_mod.make_reply_target("1234567890"),
                                 op="reply")
    asyncio.run(run())

    rows = queue.recent(limit=200)
    assert len(rows) == before + 1
    row = rows[0]
    assert reply_mod.is_reply_target(row["quote_id"]), "评论目标必须保留"
    assert reply_mod.reply_target_id(row["quote_id"]) == "1234567890"
    assert len(media_mod.parse_media_paths(row["media_path"])) == 2


def test_apply_edit_keeps_reply_target():
    """改评论文字时不能把回复目标冲掉 —— 否则评论会变成一条孤儿帖。"""
    import asyncio
    import types
    import bot
    from core import queue, reply as reply_mod

    queue.init_db()
    jid = queue.enqueue(
        tg_chat_id=1, tg_msg_id=9001, kind="text", raw_text="原评论",
        quote_id=reply_mod.make_reply_target("1234567890"),
        status="awaiting",
    )
    assert jid is not None
    msg = types.SimpleNamespace(text="改过的评论", chat_id=1, message_id=9002,
                                replies=[])

    async def _reply(text, **kw):
        msg.replies.append((text, kw))
    msg.reply_text = _reply

    asyncio.run(bot._apply_edit(jid, msg))

    row = queue.get(jid)
    assert row["raw_text"] == "改过的评论"
    assert reply_mod.is_reply_target(row["quote_id"]), "改文字后评论目标必须还在"
    assert reply_mod.reply_target_id(row["quote_id"]) == "1234567890"


# ══════════════════════════════════════════════════════════
# 推文操作流程（转帖 / 引用 / 评论）
# ══════════════════════════════════════════════════════════

def test_reply_quote_id_maps_three_ops():
    """三种操作 -> 三种 quote_id 编码；不能互相串。"""
    import bot

    base = {"target": "1234567890"}
    assert bot._reply_quote_id({**base, "op": "retweet"}) == "t:1234567890"
    assert bot._reply_quote_id({**base, "op": "reply"}) == "r:1234567890"
    assert bot._reply_quote_id({**base, "op": "quote"}) == "1234567890"
    # 缺目标或未知操作 -> 空串（调用方必须当成流程不完整，退回普通发帖是错的）
    assert bot._reply_quote_id({"op": "reply"}) == ""
    assert bot._reply_quote_id({**base, "op": ""}) == ""
    assert bot._reply_quote_id({**base, "op": "wat"}) == ""


def test_mark_reply_with_choose_stage():
    """用户直接粘链接进来：stage 直接是 choose，op 还是空的（等用户点）。"""
    import bot
    bot._reply_state.clear()
    bot._mark_reply(333, target="1234567890", stage="choose", src=42)
    st = bot._peek_reply(333)
    assert st["stage"] == "choose"
    assert st["target"] == "1234567890"
    assert st["src"] == 42
    assert st["op"] == "", "还没点操作，op 必须是空的"
    bot._take_reply(333)


def test_set_reply_op_moves_to_content_stage():
    import bot
    bot._reply_state.clear()
    bot._mark_reply(444, target="1234567890", stage="choose")
    bot._set_reply_op(444, "reply", "media")
    st = bot._peek_reply(444)
    assert st["stage"] == "content"
    assert st["op"] == "reply"
    assert st["mode"] == "media"
    bot._take_reply(444)


def test_target_line_labels_three_kinds():
    """用户看到的说明必须能分清转帖/引用/评论。"""
    import bot
    assert bot._target_line("t:123").startswith("🔄 转帖")
    assert bot._target_line("r:123").startswith("💬 评论")
    assert bot._target_line("123").startswith("✏️ 引用")
    assert bot._target_line("") == ""
    assert bot._target_line(None) == ""
    assert "https://x.com/i/status/123" in bot._target_line("t:123")


def test_job_tag_labels_retweet_not_text():
    """转帖没有正文也没媒体，绝不能显示成「📝 纯文字」。"""
    import bot

    class Row(dict):
        def keys(self):
            return super().keys()

    rt = Row(id=1, quote_id="t:123", media_path="", raw_text="", status="pending")
    assert "转帖" in bot._job_tag(rt)
    assert "纯文字" not in bot._job_tag(rt)

    rp = Row(id=2, quote_id="r:123", media_path="", raw_text="评论", status="pending")
    assert bot._job_tag(rp).startswith("💬")

    qt = Row(id=3, quote_id="123", media_path="", raw_text="引用文字", status="pending")
    assert "转帖" not in bot._job_tag(qt)
    assert not bot._job_tag(qt).startswith("💬")


def test_enqueue_retweet_creates_text_job_with_retweet_target():
    """转帖入队：kind 保持 text（没有正文），quote_id 是 t:<id>，状态是 awaiting。"""
    import asyncio
    import bot
    from core import queue, reply as reply_mod

    queue.init_db()
    bot._reply_state.clear()
    bot._mark_reply(777, target="1799888777666555444", stage="choose", src=8888)
    msg = _fake_album_msg(9999, "", gid="", chat_id=777)
    msg.photo = None       # 转帖不是相册消息
    before = len(queue.recent(limit=300))

    asyncio.run(bot._enqueue_retweet(msg, bot._peek_reply(777)))

    rows = queue.recent(limit=300)
    assert len(rows) == before + 1
    row = rows[0]
    assert reply_mod.is_retweet_target(row["quote_id"])
    assert reply_mod.retweet_target_id(row["quote_id"]) == "1799888777666555444"
    assert row["kind"] == "text"
    assert not (row["raw_text"] or "").strip(), "转帖不能有正文"
    assert not row["media_path"], "转帖不能有媒体"
    # 流程状态必须被清掉，否则下一条消息会被误当转帖
    assert bot._peek_reply(777) == {}


def test_enqueue_retweet_uses_source_message_for_dedup():
    """同一条链接消息重投 -> 唯一键拦住，不会转推两次。"""
    import asyncio
    import bot
    from core import queue

    queue.init_db()
    bot._reply_state.clear()
    bot._mark_reply(778, target="111122223333", stage="choose", src=55501)
    msg = _fake_album_msg(55501, "", gid="", chat_id=778)
    msg.photo = None

    asyncio.run(bot._enqueue_retweet(msg, bot._peek_reply(778)))
    before = len(queue.recent(limit=300))

    # Telegram 重投同一条链接消息：src 相同 -> 入队被唯一键拒绝
    bot._mark_reply(778, target="111122223333", stage="choose", src=55501)
    asyncio.run(bot._enqueue_retweet(msg, bot._peek_reply(778)))

    assert len(queue.recent(limit=300)) == before, "重投不能产生第二条转帖任务"


# ══════════════════════════════════════════════════════════
# 消息路由：直接粘链接 -> 先问「转帖 / 引用 / 评论」
# ══════════════════════════════════════════════════════════

def _fake_text_msg(text, mid=5001, chat_id=901, from_user_id=901):
    """构造一条纯文字消息 + 最小 Update（够 _on_message_inner 用）。"""
    import types

    msg = types.SimpleNamespace()
    msg.message_id = mid
    msg.chat_id = chat_id
    msg.chat = types.SimpleNamespace(id=chat_id)
    msg.text = text
    msg.caption = ""
    msg.photo = None
    msg.video = None
    msg.document = None
    msg.media_group_id = None
    msg.from_user = types.SimpleNamespace(id=from_user_id)
    msg.replies = []

    async def _reply(t, **kw):
        msg.replies.append((t, kw))
        return types.SimpleNamespace(message_id=99999)
    msg.reply_text = _reply

    upd = types.SimpleNamespace()
    upd.effective_message = msg
    upd.effective_chat = types.SimpleNamespace(id=chat_id)
    return upd, msg


def test_bare_tweet_link_asks_what_to_do_instead_of_quoting(monkeypatch):
    """核心交互：粘一条链接 -> **先问**要做什么，而不是直接当引用发出去。

    改动前这里会把链接正文里的 id 直接当成引用目标，入队一条引用推文 ——
    用户根本还没说要对这条推文做什么。
    """
    import asyncio
    import bot
    from core import queue

    queue.init_db()
    bot._reply_state.clear()
    bot._send_mode.clear()
    before = len(queue.recent(limit=400))
    upd, msg = _fake_text_msg("https://x.com/someone/status/1234567890", mid=7001, chat_id=911)

    asyncio.run(bot._on_message_inner(upd, None))

    # 1) 一条任务都不该入队 —— 还没选操作
    assert len(queue.recent(limit=400)) == before, "选操作之前不能入队"
    # 2) 等用户点操作，目标已经锁定
    st = bot._peek_reply(911)
    assert st.get("stage") == "choose"
    assert st.get("target") == "1234567890"
    # 3) 三个按钮都在
    assert msg.replies, "必须回一条「要做什么」的消息"
    text, kw = msg.replies[-1]
    markup = kw.get("reply_markup")
    datas = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "op:retweet" in datas
    assert "op:quote" in datas
    assert "op:reply" in datas
    bot._reply_state.clear()


def test_link_with_surrounding_text_still_quotes(monkeypatch):
    """链接夹在正文里（"看看这条 …… 说得对吗"）是正常的引用发帖，别拦。"""
    import asyncio
    import bot
    from core import queue

    queue.init_db()
    bot._reply_state.clear()
    bot._send_mode.clear()
    before = len(queue.recent(limit=400))
    upd, msg = _fake_text_msg("看看这条 https://x.com/someone/status/1234567890 说得对吗", mid=7002, chat_id=912)

    asyncio.run(bot._on_message_inner(upd, None))

    assert len(queue.recent(limit=400)) == before + 1, "这种应该照常入队为引用"
    assert bot._peek_reply(912) == {}, "不该进入推文操作流程"


def test_send_flow_link_is_content_not_target(monkeypatch):
    """点了「📤 发送 → 纯文字」后再发链接：那是**正文**，不是操作目标。"""
    import asyncio
    import bot
    from core import queue

    queue.init_db()
    bot._reply_state.clear()
    bot._send_mode.clear()
    bot._mark_send_mode(913, "text")
    before = len(queue.recent(limit=400))
    upd, msg = _fake_text_msg("https://x.com/someone/status/1234567890", mid=7003, chat_id=913)

    asyncio.run(bot._on_message_inner(upd, None))

    assert len(queue.recent(limit=400)) == before + 1, "发送流程里的链接应当作正文发布"
    assert bot._peek_reply(913) == {}


def test_link_stage_then_action_panel(monkeypatch):
    """点「转帖 / 引用 / 评论」入口 -> 先要链接；发链接后弹操作面板。"""
    import asyncio
    import bot

    bot._reply_state.clear()
    bot._send_mode.clear()
    bot._mark_reply(902)                 # 等价于点入口后的状态
    upd, msg = _fake_text_msg("https://x.com/a/status/9876543210", mid=6001, chat_id=902)

    asyncio.run(bot._on_message_inner(upd, None))

    st = bot._peek_reply(902)
    assert st.get("stage") == "choose"
    assert st.get("target") == "9876543210"
    text, kw = msg.replies[-1]
    datas = [b.callback_data for row in kw["reply_markup"].inline_keyboard for b in row]
    assert {"op:retweet", "op:quote", "op:reply"} <= set(datas)
    bot._reply_state.clear()


def test_not_a_link_keeps_asking_for_link():
    """带图片/乱文字时不该悄悄退出流程，而要继续要链接。"""
    import asyncio
    import bot

    bot._reply_state.clear()
    bot._mark_reply(903)
    upd, msg = _fake_text_msg("没有链接的一段话", mid=6002, chat_id=903)

    asyncio.run(bot._on_message_inner(upd, None))

    assert bot._peek_reply(903).get("stage") == "link", "还要继续等链接"
    assert any("链接" in t for t, _ in msg.replies)
    bot._reply_state.clear()


def test_retweet_stage_rejects_extra_message_instead_of_posting_it():
    """选了转帖后又发文字：绝不能静默把这段文字当成推文发出去。"""
    import asyncio
    import bot
    from core import queue

    queue.init_db()
    bot._reply_state.clear()
    bot._mark_reply(904, target="1234567890", stage="choose")
    bot._set_reply_op(904, "retweet")
    before = len(queue.recent(limit=400))
    upd, msg = _fake_text_msg("这段文字不该被发出去", mid=6003, chat_id=904)

    asyncio.run(bot._on_message_inner(upd, None))

    assert len(queue.recent(limit=400)) == before, "转帖阶段发来的文字不该变成新推文"
    assert bot._peek_reply(904) == {}, "流程应当被取消"
    assert any("不需要文字" in t for t, _ in msg.replies)


def test_text_only_reply_rejects_photo():
    """评论选了"只发文字"，发图片要拦住（不能变成图文评论）。"""
    import asyncio
    import bot
    from core import queue

    queue.init_db()
    bot._reply_state.clear()
    bot._mark_reply(905, target="1234567890", stage="choose")
    bot._set_reply_op(905, "reply", "text")
    before = len(queue.recent(limit=400))

    upd, msg = _fake_text_msg("", mid=6004, chat_id=905)
    msg.text = ""
    msg.photo = [object()]        # 有图片

    asyncio.run(bot._on_message_inner(upd, None))

    assert len(queue.recent(limit=400)) == before, "不该入队"
    assert any("只发文字" in t for t, _ in msg.replies)
    bot._reply_state.clear()


# ══════════════════════════════════════════════════════════
# 回归：机器人上点「🚀 发送」必须真的发出去（真 bug，2026-09-26 实测）
#
# 病根：`bot._pipeline` 只在 PTB 的 `post_init` 里赋值，而 PTB **只在
# `run_polling()` 内部调 post_init**。start.py / TelegramManager 走的是
# `initialize() → start() → updater.start_polling()`，这条路径 post_init
# 永不执行，于是 `get_pipeline()` 恒为 None。用户在机器人上点「发送」，
# 回调只把任务 mark 成 pending 就回一句"稍后自动发出"；而此时若
# `queue_send_enabled=0`（只手动发），主循环永远跳过它 —— 任务永久卡在
# 「待发」，点几次都没用。
# ══════════════════════════════════════════════════════════

def _fake_pub_callback(job_id, chat_id=906):
    """构造一个 `pub:<id>` 的回调 Update（够 on_callback 的 pub 分支用）。"""
    import types

    msg = types.SimpleNamespace()
    msg.chat = types.SimpleNamespace(id=chat_id)
    msg.chat_id = chat_id
    msg.message_id = 8001
    msg.replies = []

    async def _reply(t, **kw):
        msg.replies.append((t, kw))
        return types.SimpleNamespace(message_id=99998)
    msg.reply_text = _reply

    async def _edit(**kw):
        return True
    msg.edit_message_reply_markup = _edit

    q = types.SimpleNamespace()
    q.data = f"pub:{job_id}"
    q.message = msg
    q.from_user = types.SimpleNamespace(id=chat_id)

    async def _answer(*a, **kw):
        return True
    q.answer = _answer

    upd = types.SimpleNamespace()
    upd.callback_query = q
    upd.effective_message = msg
    upd.effective_chat = types.SimpleNamespace(id=chat_id)
    return upd, msg


def test_get_pipeline_falls_back_to_web_registered_instance():
    """start.py 注册在 web.server 里的常驻 pipeline，get_pipeline() 必须能取到。

    这就是病根所在：start.py 用 `set_pipeline_instance(pipe)` 注入控制台，
    但机器人回调读的是 `bot._pipeline`（post_init 才赋值，永不执行）。
    两边必须打通，否则机器人的「发送」永远拿不到 pipeline。
    """
    import importlib.util
    from web import server as web_server

    spec = importlib.util.spec_from_file_location("_bot_pipe_test", ROOT / "bot.py")
    botmod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(botmod)

    sentinel = object()
    saved_bot_pipe = botmod._pipeline
    saved_web_pipe = getattr(web_server, "_pipe_obj", None)
    try:
        botmod._pipeline = None
        web_server.set_pipeline_instance(sentinel)
        assert botmod.get_pipeline() is sentinel, (
            "get_pipeline() 必须回落到 web.server 里注册的常驻实例，"
            "否则机器人点「发送」拿不到 pipeline，任务会永久卡在待发")
    finally:
        botmod._pipeline = saved_bot_pipe
        web_server.set_pipeline_instance(saved_web_pipe)


def test_ensure_pipeline_creates_and_remembers():
    """get_pipeline() 拿不到时，ensure_pipeline() 要现场造一个并记住。"""
    import importlib.util
    from core.pipeline import Pipeline
    from web import server as web_server

    spec = importlib.util.spec_from_file_location("_bot_pipe_test2", ROOT / "bot.py")
    botmod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(botmod)

    saved_bot_pipe = botmod._pipeline
    saved_web_pipe = getattr(web_server, "_pipe_obj", None)
    try:
        botmod._pipeline = None
        web_server.set_pipeline_instance(None)
        pipe = botmod.ensure_pipeline(None)
        assert isinstance(pipe, Pipeline), "应当现场构造出 Pipeline"
        assert botmod.get_pipeline() is pipe, "构造后要记住，下次复用同一个"
    finally:
        botmod._pipeline = saved_bot_pipe
        web_server.set_pipeline_instance(saved_web_pipe)


def test_pub_callback_publishes_even_without_registered_pipeline(monkeypatch):
    """机器人点「🚀 发送」：没有常驻 pipeline 时也必须真发，不能只 mark 成 pending。

    回归的病根行为：回调里 `pipe is None` 就回一句"稍后自动发出"并 return，
    任务永远停在待发（queue_send_enabled=0 时主循环不取）。
    修好后：拿不到 pipeline 就 ensure_pipeline() 现场建一个，把这条发出去。
    """
    import asyncio
    import importlib.util
    import bot
    from core import queue, settings
    from core.backends.base import PublishResult
    from web import server as web_server

    queue.init_db()
    settings.set_many({"queue_send_enabled": "0"})   # 只手动发：主循环不取 pending

    jid = queue.enqueue(kind="text", tg_chat_id=906, tg_msg_id=8002,
                        raw_text="回归：这条必须真发出去", status="awaiting")
    assert jid is not None

    published = []

    async def fake_publish_one(self, job_id):
        published.append(int(job_id))
        queue.mark(int(job_id), "sent", tweet_id="1", tweet_url="https://x.com/i/status/1")
        return PublishResult(ok=True, backend="fake", tweet_id="1",
                             tweet_url="https://x.com/i/status/1")

    saved_bot_pipe = bot._pipeline
    saved_web_pipe = getattr(web_server, "_pipe_obj", None)
    monkeypatch.setattr(bot.Pipeline, "publish_one", fake_publish_one)
    try:
        bot._pipeline = None                          # 模拟 post_init 没跑过
        web_server.set_pipeline_instance(None)

        upd, msg = _fake_pub_callback(jid)
        asyncio.run(bot.on_callback(upd, None))

        assert published == [jid], (
            "点「发送」必须真的调用 publish_one；否则任务会永久卡在待发")
        assert queue.get(jid)["status"] == "sent", "发完状态应当是 sent"
        assert any("已发布" in t for t, _ in msg.replies), "应当回一条成功回执"
    finally:
        bot._pipeline = saved_bot_pipe
        web_server.set_pipeline_instance(saved_web_pipe)
        try:
            queue.cancel(jid)
        except Exception:
            pass
