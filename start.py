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
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import accounts, config, lock  # noqa: E402

log = logging.getLogger("twitbot.start")

# 必须持有引用：faulthandler 只保存 fd，若文件对象被 GC 回收，段错误现场就写不进去
_CRASH_FH = None


# ── 数据日报定时采集 ──
AN_COLLECT_DAYS = 92                 # 采多长历史（近三月）
AN_COLLECT_INTERVAL = 6 * 3600       # 距上次超过 6 小时才重采
AN_COLLECT_POLL = 1800               # 每 30 分钟检查一次
AN_COLLECT_FIRST_DELAY = 90          # 启动后先等一会，别跟发布抢启动瞬间


async def _analytics_loop(account_name: str | None = None) -> None:
    """定时刷新当前账号的数据日报。"""
    with accounts.use_account(account_name):
        from core import analytics

        await asyncio.sleep(AN_COLLECT_FIRST_DELAY)
        while True:
            try:
                age = analytics.collect_age_seconds()
                if age > AN_COLLECT_INTERVAL:
                    log.info("数据日报[%s]：距上次采集 %.1f 小时，开始采集…",
                             account_name or "legacy",
                             age / 3600.0 if age != float("inf") else -1.0)
                    res = await asyncio.to_thread(analytics.refresh, AN_COLLECT_DAYS)
                    if res.get("ok"):
                        log.info("数据日报[%s]：采集完成 %s", account_name or "legacy", res.get("saved"))
                    else:
                        log.warning("数据日报[%s]：采集失败 %s", account_name or "legacy", res.get("error"))
            except asyncio.CancelledError:
                log.info("数据日报定时任务退出[%s]", account_name or "legacy")
                raise
            except Exception as e:
                log.warning("数据日报定时任务异常（忽略）[%s]：%s", account_name or "legacy", e)
            await asyncio.sleep(AN_COLLECT_POLL)


REVIEW_POLL_SECONDS = 60          # 每分钟看一眼「到点没」


async def _review_loop(tg_managers) -> None:
    """每小时间隔把 1 条采集内容推给管理员审核，通过才发到 X。

    默认**关闭**（review_config.enabled=0），用户开了才跑 ——
    用户明确要求「不要直接发」。
    """
    await asyncio.sleep(150)
    while True:
        try:
            from core.collect import review as _rv
            if _rv.due():
                mgr = None
                try:
                    mgr = next(iter((tg_managers or {}).values()), None)
                except Exception:
                    mgr = None
                app = getattr(mgr, "_app", None) if mgr else None
                if app is not None:
                    from core import tgmanager as _tgm
                    try:
                        botmod = _tgm._load_bot_module()
                        ok, why = await botmod.review_push_next(app)
                        log.info("待审推送：%s（%s）", ok, why)
                    except Exception as e:
                        log.warning("待审推送异常：%s", type(e).__name__)
                else:
                    log.debug("机器人没在跑，跳过待审推送")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("待审循环异常（忽略）：%s", e)
        await asyncio.sleep(REVIEW_POLL_SECONDS)


def should_start_tg(no_tg: bool = False, force_tg: bool = False) -> bool:
    """是否拉起 Telegram 机器人。按账号读取，任一账号开启就尝试拉起。"""
    if no_tg:
        return False
    if force_tg:
        return True
    from core import settings
    for name in accounts.account_names():
        with accounts.use_account(name):
            if settings.get("tg_autostart", "1") == "1":
                return True
    return False


def _banner(mode: str, tg_state: str) -> None:
    from core import settings
    cfg = settings.all_settings()
    print("=" * 64)
    print("  twitbot —— Telegram / Web → X 自动发帖")
    print("=" * 64)
    print(f"  模式        : {mode}")
    print(f"  发布后端    : {cfg['backend']}（每个账号各自设置）")
    print(f"  数据目录    : {config.DATA_DIR}")
    print(f"  控制台账号  : {' / '.join(accounts.account_names())}（数据完全独立）")
    if mode != "worker-only":
        url = f"http://{config.WEB_HOST}:{config.WEB_PORT}"
        print(f"  控制台      : {url}")
        if config.WEB_TOKEN:
            print(f"                （需要 token，访问 {url}/?token=***）")
    print(f"  Telegram    : {tg_state}")
    print("=" * 64)


async def _run(args: argparse.Namespace) -> None:
    from core import pipeline

    accounts.install_path_router()
    accounts.init_accounts()
    account_list = list(accounts.account_names())

    tasks: list[asyncio.Task] = []
    pipes: dict[str, Any] = {}
    tg_managers: dict[str, Any] = {}

    # ── 每个账号独立发布循环 ──
    if not args.web_only:
        for account_name in account_list:
            with accounts.use_account(account_name):
                pipe = pipeline.Pipeline()
            pipes[account_name] = pipe

            async def _worker(name: str = account_name, p: Any = pipe) -> None:
                with accounts.use_account(name):
                    from core import settings
                    log.info("发布循环[%s]启动，后端=%s", name, settings.current_backend())
                    await p.run_forever()

            tasks.append(asyncio.create_task(_worker(), name=f"pipeline-{account_name}"))
            # ── 数据日报：每个账号独立采集/存储 ──
            tasks.append(asyncio.create_task(
                _analytics_loop(account_name), name=f"analytics-{account_name}"))

    # ── Telegram：每个账号独立管理自己的 token/设置/队列 ──
    # 采集内容的人工审核（每小时推 1 条，通过才发）—— 全局一个就够
    tasks.append(asyncio.create_task(_review_loop(tg_managers), name="review"))

    tg_wanted = should_start_tg(no_tg=args.no_tg, force_tg=args.tg)
    tg_state = "未拉起"
    if tg_wanted:
        from core.tgmanager import TelegramManager  # noqa: PLC0415
        states: list[str] = []
        for account_name in account_list:
            try:
                with accounts.use_account(account_name):
                    mgr = TelegramManager(
                        on_log=lambda m, n=account_name: log.info("[tg:%s] %s", n, m))
                    ok, why = await mgr.start()
                tg_managers[account_name] = mgr
                st = mgr.status()
                who = st.get("bot_username") or "?"
                if ok:
                    label = f"已启动（@{who.lstrip('@')}）" if st.get("running") else "已就绪"
                    log.info("Telegram[%s]已启动：%s", account_name, why)
                else:
                    label = f"未启动（{why}）"
                    log.warning(
                        "Telegram[%s]未启动：%s\n"
                        "        → 可在 Web 控制台的「Telegram 机器人」面板里填 token 启动，"
                        "或运行 python setup_tg.py", account_name, why)
                states.append(f"{account_name}: {label}")
            except Exception as e:
                states.append(f"{account_name}: 未启动（{type(e).__name__}）")
                log.warning("Telegram[%s]模块加载/启动异常（不影响其它账号）：%s",
                            account_name, e)
        tg_state = "；".join(states) if states else "未拉起"

    # 把常驻 pipeline 注册给 bot 模块（如 bot 支持账号参数则按账号注册）。
    for account_name, pipe in pipes.items():
        try:
            from core import tgmanager as _tgm  # noqa: PLC0415
            botmod = _tgm._load_bot_module()
            if hasattr(botmod, "set_pipeline_instance"):
                try:
                    botmod.set_pipeline_instance(pipe, username=account_name)
                except TypeError:
                    botmod.set_pipeline_instance(pipe)
                log.info("已把发布循环实例注册给 Telegram 回调[%s]", account_name)
        except Exception as e:
            log.debug("注册 pipeline 给 bot 模块失败（不影响运行）[%s]：%s",
                      account_name, e)

    # ── Web 控制台（同进程，共用事件循环）──
    server = None
    if not args.worker_only:
        import uvicorn  # noqa: PLC0415
        from web.server import create_app  # noqa: PLC0415
        from web import server as web_server  # noqa: PLC0415

        for account_name, pipe in pipes.items():
            try:
                web_server.set_pipeline_instance(pipe, account_name)
            except Exception as e:
                log.warning("注入 pipeline 实例失败[%s]：%s", account_name, e)
        for account_name, mgr in tg_managers.items():
            try:
                web_server.set_tg_manager_instance(mgr, account_name)
            except Exception as e:
                log.warning("注入 Telegram 管理器失败[%s]：%s", account_name, e)

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
    for pipe in pipes.values():
        pipe.stop()
    await asyncio.sleep(0)
    if server is not None:
        server.should_exit = True
    for account_name, mgr in tg_managers.items():
        try:
            with accounts.use_account(account_name):
                await mgr.stop()
        except Exception as e:
            log.warning("Telegram 收尾异常[%s]：%s", account_name, e)
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
    accounts.install_path_router()
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

    # 先建两个账号各自的目录/数据库；_run 里还会做幂等初始化。
    accounts.init_accounts()

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
