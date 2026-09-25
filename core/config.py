"""配置与路径（冻结）—— 从 .env 与环境变量读取；运行期可变项存 DB。

约定：
  * 本模块只负责"静态配置"（启动时定稿）。
  * 可被 Web 控制台运行时切换的项（如发布后端）放 core.settings（DB 存储）。
  * 不得 import 任何 backend —— 避免循环依赖。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# ── 路径 ──────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent.parent      # twitbot/
DATA_DIR_DEFAULT = BASE_DIR / "data"


def load_dotenv(path: Path | None = None) -> None:
    """极简 .env 加载器（Win/Linux 通用，无第三方依赖）。

    忽略空行与 # 注释；支持 KEY=VALUE 与 export KEY=VALUE；值可单/双引号包裹；
    已存在的真实环境变量优先（便于容器/CI 覆盖）。容忍 UTF-8 BOM。
    """
    p = path or (BASE_DIR / ".env")
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val


load_dotenv()


def _env(key: str, default: str = "", required: bool = False) -> str:
    v = os.getenv(key, default).strip()
    if not v and required:
        raise SystemExit(
            f"[config] 缺少必填配置: {key}\n"
            f"        请检查 {BASE_DIR / '.env'}（可从 .env.example 复制）"
        )
    return v


def _id_set(key: str) -> set[int]:
    return {int(x) for x in re.split(r"[,\s]+", _env(key)) if x.strip().lstrip("-").isdigit()}


def _int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


def _float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default


# ── Telegram ──────────────────────────────────────────────

TG_TOKEN: str = _env("TG_TOKEN")                      # 可为空：只跑 Web 控制台时不需要
ALLOWED_USERS: set[int] = _id_set("ALLOWED_USERS")
ALLOWED_CHATS: set[int] = _id_set("ALLOWED_CHATS")


def require_tg() -> str:
    """需要 Telegram 时才校验。"""
    if not TG_TOKEN:
        raise SystemExit(
            "[config] 缺少 TG_TOKEN（Telegram 功能需要）。\n"
            f"        请检查 {BASE_DIR / '.env'}，或只运行 Web 控制台：python web_app.py"
        )
    return TG_TOKEN


# ── X 官方 API 凭据 ───────────────────────────────────────

X_CONSUMER_KEY: str = _env("X_CONSUMER_KEY")
X_CONSUMER_SECRET: str = _env("X_CONSUMER_SECRET")
X_ACCESS_TOKEN: str = _env("X_ACCESS_TOKEN")
X_ACCESS_SECRET: str = _env("X_ACCESS_SECRET")


def has_x_api_creds() -> bool:
    return all([X_CONSUMER_KEY, X_CONSUMER_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET])


# ── 行为参数 ──────────────────────────────────────────────

MODE: str = _env("MODE", "auto").lower()               # auto | confirm
TWEET_PREFIX: str = _env("TWEET_PREFIX", "")
TWEET_SUFFIX: str = _env("TWEET_SUFFIX", "")
QUOTE_MODE: str = _env("QUOTE_MODE", "auto").lower()   # auto | off
DEDUP_WINDOW: int = _int("DEDUP_WINDOW", 600)
MONTHLY_LIMIT: int = _int("MONTHLY_LIMIT", 480)
MAX_ATTEMPTS: int = _int("MAX_ATTEMPTS", 5)
POLL_SECONDS: float = _float("POLL_SECONDS", 2)

# 默认发布后端（运行期可在 Web 控制台切换）
DEFAULT_BACKEND: str = _env("BACKEND", "x_api").lower()
# 浏览器后端：有头模式便于首次登录/排障
BROWSER_HEADLESS: bool = _env("BROWSER_HEADLESS", "true").lower() in ("1", "true", "yes")
BROWSER_SLOWMO: int = _int("BROWSER_SLOWMO", 0)
# Web 控制台
WEB_HOST: str = _env("WEB_HOST", "127.0.0.1")
WEB_PORT: int = _int("WEB_PORT", 8787)
WEB_TOKEN: str = _env("WEB_TOKEN", "")                 # 非空则要求 Bearer/query 校验

# ── 数据目录 ──────────────────────────────────────────────

_data_env = _env("DATA_DIR", "")
DATA_DIR: Path = Path(_data_env).expanduser() if _data_env else DATA_DIR_DEFAULT
DB_PATH: Path = DATA_DIR / "queue.db"
MEDIA_DIR: Path = DATA_DIR / "media"
BROWSER_DIR: Path = DATA_DIR / "browser"               # 浏览器登录态（storage_state）
LOG_DIR: Path = DATA_DIR / "logs"

for _d in (DATA_DIR, MEDIA_DIR, BROWSER_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def setup_console() -> None:
    """Windows cp936 控制台遇中文会 UnicodeEncodeError —— 统一切 UTF-8。"""
    import sys
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
