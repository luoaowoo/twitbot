#!/usr/bin/env python3
"""browser_login.py — 无头浏览器后端的交互式登录助手（★C）

作用：用**有头** Chromium 打开 x.com/login，让你手工登录（含两步验证），
登录成功后把 playwright storage_state 存到 `config.BROWSER_DIR/storage_state.json`，
之后 `BrowserBackend.publish()` 就能在无头模式下复用这份登录态发推。

用法（在 twitbot/ 目录下）：
    .\\.venv\\Scripts\\python.exe tools\\browser_login.py
    .\\.venv\\Scripts\\python.exe tools\\browser_login.py --timeout 600
    .\\.venv\\Scripts\\python.exe tools\\browser_login.py --check      # 只检查已保存登录态是否有效
    .\\.venv\\Scripts\\python.exe tools\\browser_login.py --state-only # 只看本地凭据文件，不联网

退出码：0=成功 / 1=失败 / 2=已保存凭据但已失效 / 3=环境不可用（无图形界面等）

注意：本脚本会真的弹出浏览器窗口。若在无图形界面的环境（纯 SSH / 无桌面会话）里运行，
Chromium 无法启动，脚本会明确报出该原因 —— 此时可在有桌面的机器上执行本脚本，
把生成的 storage_state.json 拷到 config.BROWSER_DIR 即可。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# 允许从仓库根目录直接运行（tools/ 的上一级是项目根）
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core import config  # noqa: E402

config.setup_console()

from core.backends.browser import BrowserBackend  # noqa: E402


def _echo(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="X 无头浏览器后端 —— 交互式登录助手（保存 storage_state）")
    ap.add_argument("--timeout", type=int, default=BrowserBackend.LOGIN_TIMEOUT_SECONDS,
                    help=f"等待登录的秒数上限（默认 {BrowserBackend.LOGIN_TIMEOUT_SECONDS}）")
    ap.add_argument("--check", action="store_true",
                    help="不打开登录页，只用已保存的登录态做一次连通性检查（会联网、无头）")
    ap.add_argument("--state-only", action="store_true",
                    help="只看本地凭据文件是否存在（不联网、不起浏览器）")
    ap.add_argument("--force", action="store_true",
                    help="即使已有有效登录态也重新登录")
    args = ap.parse_args(argv)

    be = BrowserBackend()
    state = be.state_path()

    print("=" * 68)
    print("  X 无头浏览器后端 —— 登录助手")
    print("=" * 68)
    print(f"项目根     : {BASE_DIR}")
    print(f"数据目录   : {config.DATA_DIR}")
    print(f"登录态文件 : {state}")
    print(f"无头模式   : {config.BROWSER_HEADLESS}（publish 时使用；登录是有头模式）")
    print("-" * 68)

    # ── 只读本地状态 ─────────────────────────────────────
    ok, reason = be.available()
    if args.state_only:
        print(f"本地凭据状态: {'可用' if ok else '不可用'} —— {reason}")
        return 0 if ok else 1

    # ── 已有凭据时的检查/提前退出 ────────────────────────
    if ok and not args.force:
        print(f"检测到已保存的登录态: {reason}")
        if not args.check:
            print("（如需更换账号重新登录，请加 --force）")
            print("正在做一次连通性检查（无头、联网）...")
        vok, vreason = be.verify()
        print(f"连通性检查: {'通过' if vok else '未通过'} —— {vreason}")
        if vok:
            print("\n✅ 登录态有效，无需重新登录。")
            return 0
        print("\n⚠ 已保存的登录态已失效，下面进入重新登录流程。")

    if args.check:
        vok, vreason = be.verify()
        print(f"连通性检查: {'通过' if vok else '未通过'} —— {vreason}")
        if not vok:
            print("\n❌ 请运行不带 --check 的本脚本重新登录。")
            return 2
        print("\n✅ 登录态有效。")
        return 0

    # ── 交互式登录 ───────────────────────────────────────
    print("\n即将打开浏览器窗口（有头模式）。请在弹出的窗口里完成登录：")
    print("  1) 输入账号 / 密码 / 两步验证码")
    print("  2) 若出现「继续 / 允许」提示按提示点掉即可")
    print(f"  3) 登录成功后本脚本会自动保存凭据；{args.timeout} 秒内未完成则超时退出")
    print("  ✳ 请勿关闭这个终端；登录完成后浏览器会自己关掉。\n")

    try:
        ok, msg = be.login(on_event=_echo, timeout=args.timeout)
    except KeyboardInterrupt:
        print("\n\n已取消（Ctrl+C）。")
        return 1

    print("-" * 68)
    if ok:
        print(f"✅ {msg}")
        print(f"   文件位置: {state}")
        print("   现在可以在 Web 控制台把发布后端切到「无头浏览器」并发一条测试推文。")
        return 0

    print(f"❌ 登录失败: {msg}")
    if not _looks_like_gui_available():
        print("   当前环境可能没有可用的图形界面，Chromium 无法以有头模式启动。")
        print("   请在有桌面的机器上执行本脚本，再把 storage_state.json 拷到上述路径。")
        return 3
    print("   请重试；若一直失败，检查网络能否访问 x.com。")
    return 1


def _looks_like_gui_available() -> bool:
    """粗判当前环境是否有图形界面（Windows/macOS 默认有；Linux 看 DISPLAY）。"""
    if sys.platform.startswith(("win", "darwin")):
        return True
    import os
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


if __name__ == "__main__":
    sys.exit(main())
