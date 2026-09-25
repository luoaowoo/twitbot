"""pytest 全局配置：保证测试永不触碰真实 data/ 目录。

`core.config` 在 **import 时**读取环境变量并定稿路径，所以必须在任何测试模块
import core 之前把 DATA_DIR 指到临时目录 —— 这正是 conftest.py 在收集阶段
就执行的作用。

同时把 TG_TOKEN / X_* 凭据清空，避免本机 .env 里的真实凭据被测试读到而导致：
  * 测试意外走真实网络请求
  * available() 返回 True 而断言期望 False
需要凭据的测试请在自己的用例内 monkeypatch 后 `importlib.reload(core.config)`。
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

# ── 在 import core 之前设置隔离环境 ──────────────────────────
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="twitbot-pytest-"))

os.environ["DATA_DIR"] = str(_TMP_ROOT)
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# 显式置空凭据：core.config.load_dotenv 只在「键不在 os.environ 中」时才写入，
# 所以置空（而不是 pop）才能挡住本机真实 .env 里的凭据渗进测试。
os.environ["TG_TOKEN"] = ""
for _k in ("X_CONSUMER_KEY", "X_CONSUMER_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET"):
    os.environ[_k] = ""
# 后端默认值也固定，避免本机 .env 干扰断言
os.environ["BACKEND"] = "x_api"
os.environ["MODE"] = "confirm"
# 关闭控制台密码门：服务器版默认会要求先登录，那会让所有既有接口测试都 401。
# 密码门本身有自己的专项测试（显式开启）。
os.environ["WEB_PASSWORD"] = "-"


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ARG001
    """整个测试会话结束后清理临时数据目录。"""
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)
