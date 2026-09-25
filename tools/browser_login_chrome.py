#!/usr/bin/env python3
"""browser_login_chrome.py — 用**真实 Chrome** 登录 X，导出 twitbot 登录态。

本脚本是 `core/chrome_login.py` 的命令行壳子。核心逻辑在那个模块里，
Web 控制台的「用真实 Chrome 登录」按钮走的是同一份实现。

## 为什么不用原来的 tools/browser_login.py

原来的助手用 **Playwright 启动浏览器**，Playwright 会带上自动化开关
（`--remote-debugging-pipe` / `--disable-features` 等），x.com 风控据此
把会话判定为机器人 —— 登录页弹「出了点问题」、URL 出现 `prelude_gate`，
**用户手工点也没用**（被标记的是浏览器进程本身）。

本脚本改用普通方式启动**系统真 Chrome**，用户正常登录，
再用 Chrome 官方调试接口（CDP）读取登录态。顺带绕开 Chrome 127+ 的
App-Bound Encryption（外部程序无法离线解密 cookie 库）。

## 用法（在 twitbot/ 目录下）

    .\\.venv\\Scripts\\python.exe tools\\browser_login_chrome.py
    .\\.venv\\Scripts\\python.exe tools\\browser_login_chrome.py --timeout 900
    .\\.venv\\Scripts\\python.exe tools\\browser_login_chrome.py --check   # 只检查已存登录态
    .\\.venv\\Scripts\\python.exe tools\\browser_login_chrome.py --reset   # 换账号（清专用配置）

退出码：0=成功 / 1=失败 / 2=已存凭据但失效 / 3=环境不可用（找不到 Chrome 等）
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core import chrome_login, config  # noqa: E402

config.setup_console()

from core.backends.browser import BrowserBackend  # noqa: E402


def _echo(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="用真实 Chrome 登录 X，并导出登录态给 twitbot（推荐给客户用）")
    ap.add_argument("--timeout", type=int, default=600,
                    help="等待登录完成的秒数上限（默认 600）")
    ap.add_argument("--check", action="store_true",
                    help="不打开浏览器，只用已保存的登录态做一次连通性检查")
    ap.add_argument("--reset", action="store_true",
                    help="清掉本工具的专用 Chrome 配置（换账号登录时用）")
    args = ap.parse_args(argv)

    be = BrowserBackend()
    state = be.state_path()
    profile = Path(config.BROWSER_DIR) / "chrome-profile"

    print("=" * 68)
    print("  X 登录助手（真实 Chrome 版）")
    print("=" * 68)
    print(f"项目根     : {BASE_DIR}")
    print(f"登录态文件 : {state}")
    print(f"Chrome     : {chrome_login.find_chrome() or '未找到'}")

    if args.check:
        ok, why = be.available()
        if not ok:
            print(f"\n❌ 本地登录态不可用：{why}")
            print("   请运行不带 --check 的本脚本登录一次。")
            return 2
        vok, vwhy = be.verify()
        print(f"\n连通性检查: {'通过' if vok else '未通过'} —— {vwhy}")
        return 0 if vok else 2

    if args.reset and profile.exists():
        shutil.rmtree(profile, ignore_errors=True)
        print(f"已清掉专用配置: {profile}")

    if not chrome_login.find_chrome():
        print("\n❌ 找不到 Chrome。请安装 Google Chrome，"
              "或用环境变量 CHROME_PATH 指定 chrome.exe 的完整路径。")
        return 3

    print("-" * 68)
    print("即将打开一个 Chrome 窗口，请在那里正常登录 X：")
    print("  1) 输入账号 / 密码（两步验证、人机验证都能正常过）")
    print("  2) 登录成功后**不用手动关闭窗口**，脚本会自动检测并保存")
    print(f"  3) {args.timeout} 秒内未完成则超时退出")
    print()
    print("  这个窗口用的是专用配置，不影响你日常用的 Chrome。")
    print("  登录一次即可长期有效；换账号请加 --reset。\n")

    try:
        ok, msg = chrome_login.login_via_chrome(
            state_path=state, profile_dir=profile,
            timeout=args.timeout, on_event=_echo)
    except KeyboardInterrupt:
        print("\n\n已取消（Ctrl+C）。")
        return 1

    print("-" * 68)
    if ok:
        print(f"✅ {msg}")
        print("   现在可以在 Web 控制台把发布后端切到「无头浏览器」并发一条测试推文。")
        return 0
    print(f"❌ {msg}")
    return 3 if "找不到 Chrome" in msg else 1


if __name__ == "__main__":
    raise SystemExit(main())
