"""控制台便捷启动脚本（★D，可选）—— 等价于 `python web/server.py`。

用法：
    .venv\\Scripts\\python.exe tools\\web_run.py
    .venv\\Scripts\\python.exe tools\\web_run.py --port 8899 --host 127.0.0.1

说明：真正的应用工厂在 `web/server.py:create_app()`（统一启动器 start.py 也复用它）。
本脚本只是给不习惯记长命令的人一个入口，不做多余的事。
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import config  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="twitbot Web 控制台启动器")
    ap.add_argument("--host", default=None, help=f"监听地址（默认 {config.WEB_HOST}，建议保持本地）")
    ap.add_argument("--port", type=int, default=None, help=f"监听端口（默认 {config.WEB_PORT}）")
    ap.add_argument("--reload", action="store_true", help="开发用：代码改动自动重启")
    args = ap.parse_args()

    config.setup_console()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    import uvicorn

    from web.server import create_app

    host = args.host or config.WEB_HOST
    port = int(args.port or config.WEB_PORT)
    tok = (config.WEB_TOKEN or "").strip()

    print("=" * 62)
    print("  twitbot 控制台")
    print(f"  地址     : http://{host}:{port}/")
    if tok:
        print(f"  带 token : http://{host}:{port}/?token={tok}")
    print(f"  数据目录 : {config.DATA_DIR}")
    print(f"  访问校验 : {'已启用 WEB_TOKEN' if tok else '未启用（仅建议本地访问）'}")
    print("=" * 62)

    if args.reload:
        # reload 模式必须传 import 字符串（不能传 app 实例）
        uvicorn.run("web.server:create_app", factory=True, host=host, port=port,
                    log_level="info", reload=True)
    else:
        uvicorn.run(create_app(), host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
