#!/usr/bin/env python3
"""twitbot-cli.py — 服务器版命令行工具（无图形界面环境专用）

服务器上没有桌面，弹不出 Chrome 窗口，所以登录必须在**别的机器**上做，
再把登录态搬过来。本工具就是干这个的：

    python twitbot-cli.py state import <storage_state.json>   # 导入登录态
    python twitbot-cli.py state show                          # 看当前登录态（不含内容）
    python twitbot-cli.py state check                         # 联网检查登录态是否有效
    python twitbot-cli.py status                              # 队列 / 配额 / 后端
    python twitbot-cli.py backend                             # 看后端可用性
    python twitbot-cli.py post --text "内容" [--media a.jpg]  # 直接投料入队
    python twitbot-cli.py queue [--limit 20]                  # 看队列
    python twitbot-cli.py start [--port 8787] --no-tg         # 启动服务

退出码：0=成功 / 1=失败 / 2=参数或状态问题
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core import config  # noqa: E402

config.setup_console()

from core import media as media_mod  # noqa: E402
from core import queue, settings, textutil  # noqa: E402
from core.backends import registry  # noqa: E402

# 首次运行必须先把库/表建好，否则任何涉及队列的命令都会
# "sqlite3.OperationalError: no such table: jobs"
try:
    queue.init_db()
    settings.all_settings()      # 顺带建 settings 表
except Exception as _e:          # 建库失败不该让 --help 之类的命令也用不了
    print(f"⚠ 初始化数据库失败：{type(_e).__name__}: {_e}", file=sys.stderr)


# ══════════════════════════════════════════════════════════
# state：登录态管理（服务器部署的核心）
# ══════════════════════════════════════════════════════════

def _state_path() -> Path:
    from core.backends.browser import BrowserBackend
    return BrowserBackend().state_path()


def cmd_state_import(args) -> int:
    """导入（覆盖）登录态文件。会先校验格式与 auth_token。"""
    src = Path(args.path)
    if not src.exists():
        print(f"❌ 找不到文件：{src}")
        return 2
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"❌ 不是合法 JSON：{type(e).__name__}: {e}")
        return 2
    if not isinstance(data, dict) or "cookies" not in data:
        print("❌ 格式不对：应传入 Playwright 的 storage_state.json（需含 cookies）")
        return 2

    cookies = [c for c in (data.get("cookies") or []) if isinstance(c, dict)]
    names = {c.get("name") for c in cookies}
    if "auth_token" not in names:
        print("❌ 这份登录态里没有 auth_token —— 很可能是没登录成功就导出了")
        print(f"   当前含有的 cookie：{sorted(n for n in names if n)}")
        return 2

    dst = _state_path()
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(dst, 0o600)
    except Exception:
        pass
    print(f"✅ 登录态已导入（{len(cookies)} 条 cookie）")
    print(f"   位置：{dst}")
    # 导入的登录态里没有账号名（storage_state 不含它），提示去 check 识别
    print("   提示：跑 `state check` 既能验证是否可用，也会自动识别出账号是谁。")
    return 0


def cmd_state_show(args) -> int:
    """显示登录态概况（**绝不打印 cookie 内容**）。"""
    p = _state_path()
    if not p.exists():
        print(f"❌ 尚未导入登录态（应位于 {p}）")
        print("   在有桌面的机器上登录后，用 `state import <文件>` 导入。")
        return 2
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        cookies = [c for c in (data.get("cookies") or []) if isinstance(c, dict)]
        names = sorted({str(c.get('name')) for c in cookies if c.get("name")})
        who = settings.get("x_username", "") or ""
        print("✅ 登录态存在")
        # 账号名放最前面 —— 用户最想知道"我登的是谁"
        print(f"   账号：{who if who else '未知（未记录，跑 state check 可自动识别）'}")
        print(f"   位置：{p}")
        print(f"   大小：{p.stat().st_size} B")
        print(f"   cookie 数：{len(cookies)}")
        print(f"   含 auth_token：{'auth_token' in names}")
        print(f"   关键 cookie：{[n for n in names if n in ('auth_token','ct0','twid')]}")
        return 0
    except Exception as e:
        print(f"❌ 文件存在但读不出来：{type(e).__name__}: {e}")
        return 2


def cmd_state_why(args) -> int:
    """解释为什么服务器上不能直接登录（实测结论，不是猜测）。"""
    from core import chrome_login
    print("=" * 64)
    print("  为什么服务器上不能直接登录 X")
    print("=" * 64)
    print()
    print("【原因一】没有图形界面，弹不出登录窗口")
    print(f"  当前环境有显示界面：{chrome_login.has_display()}")
    print("  浏览器登录要弹出真实窗口让你输密码 / 过验证码。")
    print("  服务器（无桌面 / SSH）没有显示器 —— 这是物理限制。")
    print()
    print("【原因二】无头模式会被 X 拒绝（实测 2026-09）")
    print("  试过把登录改成无头跑，结果是：")
    print("    · channel=chromium / chrome -> HTTP 403，页面全空")
    print("    · 裸 headless=True            -> HTTP 403，页面全空")
    print("  连续 3 次采样，页面元素数恒为 0 —— 拿不到登录表单，")
    print("  所以账号密码根本没地方填。")
    print()
    print("【原因三】X 对自动化登录本身有风控")
    print("  即使有桌面，全自动填账号密码也可能被要求人机验证。")
    print()
    print("=" * 64)
    print("  正确做法")
    print("=" * 64)
    print()
    print("  ① 在有桌面的机器上（Windows / macOS / 带桌面的 Linux）")
    print("     运行：python tools/browser_login_chrome.py")
    print("     在弹出的 Chrome 里正常登录一次")
    print()
    print("  ② 把生成的 data/browser/storage_state.json 传到服务器")
    print()
    print("  ③ 在服务器上导入并验证：")
    print("     python twitbot-cli.py state import /tmp/state.json")
    print("     python twitbot-cli.py state check")
    print()
    print("  详细步骤见 deploy/LINUX-DEPLOY.md")
    print()
    print("【另一条路】用 X 官方 API（完全不用浏览器）")
    print("  在 developer.x.com 申请应用，把四串密钥填进 .env：")
    print("    X_CONSUMER_KEY / X_CONSUMER_SECRET")
    print("    X_ACCESS_TOKEN / X_ACCESS_SECRET")
    print("  然后用 python setup_x.py 校准。这条路不受图形界面限制。")
    return 0


def cmd_state_check(args) -> int:
    """联网检查登录态是否真的有效（会起无头浏览器）。"""
    from core.backends.browser import BrowserBackend
    be = BrowserBackend()
    ok, why = be.available()
    print(f"本地凭据：{'可用' if ok else '不可用'} —— {why}")
    if not ok:
        return 2
    print("正在联网检查（会在无头浏览器里打开 x.com）…")
    vok, vwhy = be.verify()
    print(f"连通性：{'通过 ✅' if vok else '未通过 ❌'} —— {vwhy}")
    if vok:
        who = settings.get("x_username", "") or ""
        if who:
            print(f"当前账号：{who}")
        else:
            print("当前账号：未能识别（X 前端可能改版）")
    return 0 if vok else 2


# ══════════════════════════════════════════════════════════
# 常规运维
# ══════════════════════════════════════════════════════════

def cmd_status(args) -> int:
    st = queue.stats()
    print("=== 队列 ===")
    print(f"  总数：{st.get('total')}")
    for k, v in (st.get("by_status") or {}).items():
        print(f"    {k}: {v}")
    print(f"  本月已发：{st.get('month_sent')}/{st.get('limit')}")
    print()
    print("=== 运行设置 ===")
    print(f"  后端：{settings.current_backend()}")
    print(f"  暂停：{settings.is_paused()}")
    print(f"  队列自动发送：{settings.queue_send_enabled()}")
    print(f"  发送间隔：{settings.get_int('send_interval_minutes', 0)} 分钟")
    return 0


def cmd_backend(args) -> int:
    print("=== 发布后端 ===")
    for it in registry.describe():
        flag = "✅" if it["available"] else "⛔"
        cur = " <-当前" if it["name"] == settings.current_backend() else ""
        print(f"  {flag} {it['label']}（{it['name']}）{cur}")
        if not it["available"]:
            print(f"      {it['reason']}")
    return 0


def cmd_post(args) -> int:
    """投料入队（等价于控制台的「提交到队列」）。"""
    text = args.text or ""
    media_field = ""
    if args.media:
        names = []
        for m in args.media:
            p = Path(m)
            if not p.exists():
                print(f"❌ 媒体不存在：{p}")
                return 2
            # 已在 MEDIA_DIR 里的直接用；否则复制进去
            try:
                if p.parent.resolve() == Path(config.MEDIA_DIR).resolve():
                    names.append(p.name)
                else:
                    ok, err, rel = media_mod.save_upload(p.name, p.read_bytes())
                    if not ok:
                        print(f"❌ {p.name}：{err}")
                        return 2
                    names.append(rel)
            except Exception as e:
                print(f"❌ {p.name} 处理失败：{type(e).__name__}: {e}")
                return 2
        media_field = media_mod.pack_media_paths(names)
    if not text.strip() and not media_field:
        print("❌ 至少要给 --text 或 --media 之一")
        return 2

    quote_id = textutil.first_tweet_id(text) or ""
    h = textutil.content_hash("photo" if media_field else "text", text, quote_id, media_field)
    status = "pending" if args.send else "awaiting"
    jid = queue.enqueue(kind="photo" if media_field else "text", raw_text=text,
                        media_path=media_field, quote_id=quote_id,
                        content_hash=h, status=status)
    if jid is None:
        print("⚠ 已存在相同任务（按 chat,msg 去重），未重复入队")
        return 0
    print(f"✅ 已入队 #{jid}（状态 {status}）"
          f"{'，等发布循环取走' if status == 'pending' else '，需在控制台点发送'}")
    return 0


def cmd_queue(args) -> int:
    rows = queue.recent(limit=args.limit)
    if not rows:
        print("（队列为空）")
        return 0
    print(f"{'ID':>5}  {'状态':<9} {'类型':<7}  内容")
    for r in rows:
        paths = media_mod.parse_media_paths(r["media_path"] or "")
        kind = f"{len(paths)}图" if paths else "文字"
        txt = (r["raw_text"] or "").replace("\n", " ")[:34]
        flag = f" ⚠ {str(r['error'])[:40]}" if r["error"] else ""
        print(f"{r['id']:>5}  {r['status']:<9} {kind:<7}  {txt}{flag}")
    return 0


def cmd_start(args) -> int:
    """转交给统一启动器（服务器上通常只跑 Web + 发布循环）。"""
    import subprocess
    cmd = [sys.executable, str(BASE_DIR / "start.py")]
    if args.no_tg:
        cmd.append("--no-tg")
    if args.web_only:
        cmd.append("--web-only")
    if args.worker_only:
        cmd.append("--worker-only")
    env = {**os.environ}
    if args.host:
        env["WEB_HOST"] = args.host
    if args.port:
        env["WEB_PORT"] = str(args.port)
    return subprocess.call(cmd, env=env)


# ══════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="twitbot-cli",
        description="twitbot 服务器版命令行工具（无图形界面）")
    sub = ap.add_subparsers(dest="cmd")

    p_state = sub.add_parser("state", help="登录态管理")
    ssub = p_state.add_subparsers(dest="action")
    p_imp = ssub.add_parser("import", help="导入 storage_state.json")
    p_imp.add_argument("path")
    ssub.add_parser("show", help="看登录态概况")
    ssub.add_parser("check", help="联网检查登录态是否有效")
    ssub.add_parser("why", help="为什么服务器上不能登录（看这个就懂了）")

    sub.add_parser("status", help="队列 / 配额 / 设置")
    sub.add_parser("backend", help="看后端可用性")

    p_post = sub.add_parser("post", help="投料入队")
    p_post.add_argument("--text", default="")
    p_post.add_argument("--media", action="append", default=[])
    p_post.add_argument("--send", action="store_true",
                        help="直接排队（默认进「待确认」，需手动放行）")

    p_q = sub.add_parser("queue", help="看队列")
    p_q.add_argument("--limit", type=int, default=20)

    p_s = sub.add_parser("start", help="启动服务")
    p_s.add_argument("--host")
    p_s.add_argument("--port", type=int)
    p_s.add_argument("--no-tg", action="store_true")
    p_s.add_argument("--web-only", action="store_true")
    p_s.add_argument("--worker-only", action="store_true")

    args = ap.parse_args(argv)

    if args.cmd == "state":
        if args.action == "why":
            return cmd_state_why(args)
        if args.action == "import":
            return cmd_state_import(args)
        if args.action == "show":
            return cmd_state_show(args)
        if args.action == "check":
            return cmd_state_check(args)
        p_state.print_help()
        return 2
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "backend":
        return cmd_backend(args)
    if args.cmd == "post":
        return cmd_post(args)
    if args.cmd == "queue":
        return cmd_queue(args)
    if args.cmd == "start":
        return cmd_start(args)

    ap.print_help()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已取消。")
        raise SystemExit(1)
