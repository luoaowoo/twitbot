#!/usr/bin/env python3
"""统一启动器 —— 一条命令把全部组件拉起来。

用法：
    python start.py               # 【默认】Web 控制台 + 发布循环 + Telegram 机器人
    python start.py --no-tg       # 不拉起 Telegram 机器人（覆盖设置）
    python start.py --tg          # 强制拉起 Telegram 机器人（覆盖设置）
    python start.py --no-web      # 不起 Web 控制台（纯后台跑）
    python start.py --web-only    # 只起 Web 控制台
    python start.py --worker-only # 只起发布循环

设计：全部跑在**同一个进程、同一个事件循环**里，因此：
  · Web 上的「暂停 / 切换后端」立刻影响正在跑的发布循环；
  · Web 上的「启动/停止机器人」直接控制的就是这个进程里的 Telegram 长轮询；
  · 控制台与机器人共用同一个队列，互不打架。

Telegram 没有配 token 时**不会**导致启动失败 —— 只在控制台提示，可稍后
在 Web 界面里填 token 直接启动。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import config, lock, queue  # noqa: E402

log = logging.getLogger("twitbot.start")

# 必须持有引用：faulthandler 只保存 fd，若文件对象被 GC 回收，段错误现场就写不进去
_CRASH_FH = None


def should_start_tg(no_tg: bool = False, force_tg: bool = False) -> bool:
    """是否拉起 Telegram 机器人。

    优先级：`--no-tg` > `--tg` > 控制台设置 `tg_autostart`。
    默认值在 `core.settings.DEFAULTS` 里（"1" = 开），所以不传 fallback。
    """
    if no_tg:
        return False
    if force_tg:
        return True
    from core import settings
    return settings.get("tg_autostart", "1") == "1"


def _banner(mode: str, tg_state: str) -> None:
    from core import settings
    cfg = settings.all_settings()
    print("=" * 64)
    print("  twitbot —— Telegram / Web → X 自动发帖")
    print("=" * 64)
    print(f"  模式        : {mode}")
    print(f"  发布后端    : {cfg['backend']}")
    print(f"  数据目录    : {config.DATA_DIR}")
    if mode != "worker-only":
        url = f"http://{config.WEB_HOST}:{config.WEB_PORT}"
        print(f"  控制台      : {url}")
        if config.WEB_TOKEN:
            print(f"                （需要 token，访问 {url}/?token=***）")
    print(f"  Telegram    : {tg_state}")
    print("=" * 64)


async def _run(args: argparse.Namespace) -> None:
    from core import pipeline, settings

    tasks: list[asyncio.Task] = []
    pipe = None
    tg_mgr = None

    # ── 发布循环 ──
    if not args.web_only:
        pipe = pipeline.Pipeline()

        async def _worker() -> None:
            log.info("发布循环启动，后端=%s", settings.current_backend())
            await pipe.run_forever()

        tasks.append(asyncio.create_task(_worker(), name="pipeline"))

    # ── Telegram 机器人（没 token 只警告不致命）──
    # 是否拉起：--no-tg / --tg 显式指定优先，否则跟随控制台里的
    # 「启动 twitbot 时自动拉起机器人」设置（tg_autostart，默认开）。
    tg_wanted = should_start_tg(no_tg=args.no_tg, force_tg=args.tg)

    tg_state = "未拉起"
    if tg_wanted:
        try:
            from core.tgmanager import TelegramManager  # noqa: PLC0415
            tg_mgr = TelegramManager(on_log=lambda m: log.info("[tg] %s", m))
            ok, why = await tg_mgr.start()
            if ok:
                st = tg_mgr.status()
                who = st.get("bot_username") or "?"
                tg_state = f"已启动（@{who.lstrip('@')}）" if st.get("running") else "已就绪"
                log.info("Telegram 机器人已启动：%s", why)
            else:
                tg_state = f"未启动（{why}）"
                log.warning(
                    "Telegram 未启动：%s\n"
                    "        → 可在 Web 控制台的「Telegram 机器人」面板里填 token 启动，"
                    "或运行 python setup_tg.py", why)
        except Exception as e:
            tg_state = f"未启动（{type(e).__name__}）"
            log.warning("Telegram 模块加载/启动异常（不影响 Web 与发布）：%s", e)

    # ── Web 控制台（同进程，共用事件循环）──
    server = None
    if not args.worker_only:
        import uvicorn  # noqa: PLC0415
        from web.server import create_app  # noqa: PLC0415
        from web import server as web_server  # noqa: PLC0415

        # 把正在跑的实例注入控制台：控制台的「立即发送」复用同一个循环实例，
        # 「启停机器人」控制的也是同一个 Telegram 轮询。
        if pipe is not None and hasattr(web_server, "set_pipeline_instance"):
            try:
                web_server.set_pipeline_instance(pipe)
                log.info("已把发布循环实例注入 Web 控制台")
            except Exception as e:
                log.warning("注入 pipeline 实例失败（控制台将自建，不影响运行）：%s", e)
        if tg_mgr is not None and hasattr(web_server, "set_tg_manager_instance"):
            try:
                web_server.set_tg_manager_instance(tg_mgr)
                log.info("已把 Telegram 管理器注入 Web 控制台")
            except Exception as e:
                log.warning("注入 Telegram 管理器失败（控制台将自建，不影响运行）：%s", e)

        app = create_app()
        cfg = uvicorn.Config(
            app, host=config.WEB_HOST, port=config.WEB_PORT,
            log_level="info", loop="asyncio",
        )
        server = uvicorn.Server(cfg)
        tasks.append(asyncio.create_task(server.serve(), name="web"))

    _banner(
        "worker-only" if args.worker_only else (
            "web+worker" + ("+tg" if tg_wanted else "")),
        tg_state,
    )

    # ── 等待任一任务结束（或 Ctrl+C）──
    stop = asyncio.Event()

    def _signal(*_: object) -> None:
        log.info("收到退出信号，正在收尾……")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal)
        except (NotImplementedError, ValueError, AttributeError):
            # Windows 的 ProactorEventLoop 不支持 add_signal_handler
            try:
                signal.signal(sig, _signal)
            except (ValueError, OSError):
                pass

    done, _pending = await asyncio.wait(
        [asyncio.create_task(stop.wait()), *tasks],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for t in done:
        exc = t.exception() if not t.cancelled() else None
        if exc:
            log.error("任务 %s 异常退出：%s", t.get_name(), exc)

    # ── 收尾 ──
    if pipe is not None:
        pipe.stop()          # 请求发布循环优雅退出（不打断正在发的这一条）
        await asyncio.sleep(0)
    if server is not None:
        server.should_exit = True
    if tg_mgr is not None:
        try:
            await tg_mgr.stop()
        except Exception as e:
            log.warning("Telegram 收尾异常：%s", e)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    log.info("已退出")


def _setup_logging() -> None:
    """控制台 + 文件双写日志，并捕获崩溃现场。

    为什么必须有文件日志：此前日志只进 stdout，进程一死（被强杀、崩溃、
    终端关掉）全部现场消失，`data/logs/` 永远空着 —— 出问题只能靠猜。
    现在任何异常退出都会在 data/logs/ 留下 traceback。
    """
    log_path = config.LOG_DIR / "twitbot.log"
    handlers: list[logging.Handler] = [logging.StreamHandler()]

    try:
        from logging.handlers import RotatingFileHandler
        handlers.append(RotatingFileHandler(
            log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"))
    except Exception:
        pass   # 磁盘只读等极端情况：退回纯控制台，不影响启动

    logging.basicConfig(
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        level=logging.INFO,
        handlers=handlers,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # 段错误等硬崩溃也要留证据（否则强杀 / ctypes 崩溃无迹可寻）。
    # 必须持有文件引用：faulthandler 只保存 fd，文件对象被 GC 回收后现场就丢了。
    global _CRASH_FH
    try:
        import faulthandler
        _CRASH_FH = open(config.LOG_DIR / "crash.log", "a", encoding="utf-8")
        faulthandler.enable(file=_CRASH_FH, all_threads=True)
    except Exception:
        _CRASH_FH = None

    def _excepthook(etype, value, tb) -> None:
        logging.getLogger("twitbot").critical(
            "未捕获异常，进程即将退出", exc_info=(etype, value, tb))

    sys.excepthook = _excepthook
    log.info("日志文件：%s", log_path)


def main() -> None:
    config.setup_console()
    ap = argparse.ArgumentParser(
        description="twitbot 统一启动器（默认拉起 Web + 发布循环 + Telegram）")
    ap.add_argument("--no-tg", action="store_true", help="不拉起 Telegram 机器人")
    ap.add_argument("--tg", action="store_true", help="强制拉起 Telegram 机器人（忽略设置）")
    ap.add_argument("--no-web", action="store_true", help="不起 Web 控制台")
    ap.add_argument("--web-only", action="store_true", help="只起 Web 控制台（不发帖）")
    ap.add_argument("--worker-only", action="store_true", help="只起发布循环（无界面）")
    args = ap.parse_args()

    if args.web_only and args.worker_only:
        raise SystemExit("--web-only 与 --worker-only 不能同时用")
    if args.no_web and args.web_only:
        raise SystemExit("--no-web 与 --web-only 冲突")
    if args.no_web:
        args.worker_only = True

    _setup_logging()

    queue.init_db()
    from core import settings as _s
    _s.all_settings()      # 建 settings 表

    lk = lock.single_instance_lock()
    if lk is None:
        raise SystemExit(
            "[lock] 已有实例在运行（或上次异常退出残留锁）。\n"
            f"       锁文件: {config.DATA_DIR / 'bot.lock'}\n"
            f"       持有者: {lock.lock_holder() or '未知'}\n"
            "       确认无其它进程后删除该锁文件再启动。"
        )

    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass
    finally:
        try:
            lk.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
