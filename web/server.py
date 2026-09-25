"""Web 控制台后端（★D）—— FastAPI 应用。

职责：把 core 的队列/设置/后端注册表暴露成 HTTP API，供单页控制台使用。

硬性约束（见 AGENT_CONTRACT.md §6）：
  * **绝不 import bot.py**（会拉起 Telegram 长轮询）；只依赖 core。
  * 默认只绑 127.0.0.1；config.WEB_TOKEN 非空时校验 Bearer / ?token=。
  * 后端模块（x_api.py / browser.py）缺失时服务必须照常启动，
    可用性由 registry.describe() 的 loaded=false + reason 表达。
  * 一切错误返回 JSON：{"error": "中文说明"} + 合适状态码。

对外入口：
    create_app() -> FastAPI      给统一启动器/uvicorn 用（start.py 同进程挂载）
    python web/server.py         直接跑（内部调 uvicorn.run）
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import logging
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 允许 `python web/server.py` 直接运行时找到项目根（twitbot/）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi import FastAPI, File, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, Response,
                               StreamingResponse)  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from core import config, queue, settings, textutil  # noqa: E402
from core.backends import registry  # noqa: E402
from core.backends.base import Job  # noqa: E402

log = logging.getLogger("twitbot.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_FILE = STATIC_DIR / "index.html"

WEB_VERSION = "1.0.0"
# 页面标识：附带进程启动时间，用于区分"浏览器加载的是新页面还是内存里的旧页面"。
# 单页应用只在打开时取一次脚本，改完代码不刷新页面就会一直跑旧逻辑 —— 有个
# 会变的标识能立刻看出来。启动时定稿，运行期不变。
WEB_BUILD = time.strftime("%m%d-%H%M")

# 可经 /api/settings 修改的白名单（不含 backend/paused：各有专用接口做校验）
SETTINGS_WRITABLE = ("tweet_prefix", "tweet_suffix", "monthly_limit", "dedup_window",
                     "quote_mode", "tg_allowed_users", "tg_allowed_chats", "tg_autostart")

VALID_STATUS = ("pending", "awaiting", "sent", "failed", "canceled", "duplicate")
VALID_KINDS = ("text", "photo", "video", "document")

MEDIA_EXT_KIND = {
    ".jpg": "photo", ".jpeg": "photo", ".png": "photo", ".gif": "photo",
    ".webp": "photo", ".bmp": "photo",
    ".mp4": "video", ".mov": "video", ".mkv": "video", ".webm": "video",
}


# ══════════════════════════════════════════════════════════
# 后端可用性缓存（describe 会真的去 import 后端模块，别每个请求都做）
# ══════════════════════════════════════════════════════════

_DESC_TTL = 2.0
_desc_cache: dict[str, Any] = {"at": 0.0, "data": []}
_desc_lock = threading.Lock()


def invalidate_backends_cache() -> None:
    with _desc_lock:
        _desc_cache["at"] = 0.0


def backends_view(force: bool = False) -> list[dict]:
    """registry.describe() 的带 TTL 缓存版本（单后端故障不影响其它后端）。"""
    now = time.time()
    with _desc_lock:
        if not force and _desc_cache["data"] and now - _desc_cache["at"] < _DESC_TTL:
            return copy.deepcopy(_desc_cache["data"])
    try:
        data = registry.describe()
    except Exception as e:  # describe 自身已容错，这里只是最后一道防线
        log.exception("registry.describe 失败")
        data = [{"name": n, "label": n, "available": False, "loaded": False,
                 "reason": f"注册表异常: {type(e).__name__}: {e}"}
                for n in ("x_api", "browser")]
    with _desc_lock:
        _desc_cache["at"] = time.time()
        _desc_cache["data"] = data
    return copy.deepcopy(data)


def get_backend(name: str):
    """按名取后端实例。模块缺失/实例化失败时抛 HTTPException(400)。"""
    name = (name or "").strip().lower()
    if name not in registry.REGISTRY:
        raise HTTPException(400, f"未知后端 {name!r}；可选：{'/'.join(registry.REGISTRY)}")
    try:
        return registry.get(name)
    except Exception as e:
        raise HTTPException(400, f"后端 {name} 模块未就绪：{type(e).__name__}: {e}") from e


def backend_available_or_400(name: str) -> tuple[Any, bool, str]:
    """取后端实例并检查 available()。返回 (实例, 是否可用, 原因)。"""
    be = get_backend(name)
    try:
        ok, reason = be.available()
    except Exception as e:  # 契约要求不抛异常，这里兜底
        ok, reason = False, f"available() 异常：{type(e).__name__}: {e}"
    return be, bool(ok), str(reason or "")


# ══════════════════════════════════════════════════════════
# 浏览器登录后台任务状态（不阻塞请求）
# ══════════════════════════════════════════════════════════

_LOGIN_MAX_EVENTS = 300
_login_lock = threading.Lock()
_login_state: dict[str, Any] = {
    "running": False,
    "backend": "",
    "ok": None,          # None=进行中/未开始；True/False=结束态
    "message": "",
    "events": [],        # [{"t": iso, "msg": str}]
    "started_at": "",
    "finished_at": "",
    "need_code": False,  # 账号密码登录：X 要求二次验证码（≠失败）
}


def _login_reset(name: str) -> None:
    with _login_lock:
        _login_state.update(
            running=True, backend=name, ok=None, message="正在启动登录流程…",
            events=[], started_at=queue.now_iso(), finished_at="", need_code=False,
        )


def _login_event(msg: str) -> None:
    with _login_lock:
        ev = _login_state["events"]
        ev.append({"t": queue.now_iso(), "msg": str(msg)})
        if len(ev) > _LOGIN_MAX_EVENTS:
            del ev[: len(ev) - _LOGIN_MAX_EVENTS]
        _login_state["message"] = str(msg)


def _login_finish(ok: bool, message: str) -> None:
    with _login_lock:
        _login_state.update(running=False, ok=bool(ok), message=str(message),
                            finished_at=queue.now_iso())


def login_status() -> dict:
    with _login_lock:
        snap = copy.deepcopy(_login_state)
    if snap.get("started_at"):
        try:
            t0 = datetime.fromisoformat(snap["started_at"])
            t1 = datetime.fromisoformat(snap["finished_at"] or queue.now_iso())
            snap["elapsed"] = max(0, int((t1 - t0).total_seconds()))
        except Exception:
            snap["elapsed"] = 0
    else:
        snap["elapsed"] = 0
    return snap


def _login_worker(name: str) -> None:
    """后台线程跑阻塞式 login()，事件推进内存状态供前端轮询。"""
    try:
        be = registry.get(name)
        if not hasattr(be, "login"):
            _login_finish(False, f"后端 {name} 未实现 login()")
            return
        _login_event(f"已调用 {name}.login()，浏览器窗口会打开，请在其中完成登录")
        try:
            ok, msg = be.login(on_event=_login_event)
        except TypeError:
            # 后端 login() 未接受 on_event 参数时的兼容路径
            _login_event("该后端 login() 不支持 on_event 回调，改用静默模式")
            ok, msg = be.login()
        _login_finish(bool(ok), str(msg or ("登录成功" if ok else "登录失败")))
    except Exception as e:
        log.exception("浏览器登录任务失败")
        _login_event(f"异常：{type(e).__name__}: {e}")
        _login_finish(False, f"登录异常：{type(e).__name__}: {e}")
    finally:
        invalidate_backends_cache()


def _login_chrome_worker(name: str) -> None:
    """用**真实 Chrome**（CDP）登录 —— 绕开 Playwright 的自动化指纹。

    背景：`BrowserBackend.login()` 用 Playwright 启动浏览器，Playwright 会带上
    `--remote-debugging-pipe` / `--disable-features` 等开关，x.com 风控据此
    把会话判定为机器人（登录页弹「出了点问题」、URL 出现 `prelude_gate`），
    **用户手工点也没用**。这条路改用普通方式启动系统真 Chrome，
    用户正常登录后再用 Chrome 官方调试接口读取登录态。
    """
    try:
        from core import chrome_login  # 延迟导入：未装 websockets 时不拖垮控制台
        be = registry.get(name)
        state = be.state_path() if hasattr(be, "state_path") else None
        if state is None:
            _login_finish(False, f"后端 {name} 没有 state_path()，无法保存登录态")
            return
        profile = state.parent / "chrome-profile"
        _login_event("正在启动真实 Chrome，请在弹出的窗口里正常登录 X……")
        ok, msg = chrome_login.login_via_chrome(
            state_path=state, profile_dir=profile,
            on_event=_login_event)
        _login_finish(bool(ok), str(msg))
    except Exception as e:
        log.exception("真实 Chrome 登录任务失败")
        _login_event(f"异常：{type(e).__name__}: {e}")
        _login_finish(False, f"登录异常：{type(e).__name__}: {e}")
    finally:
        invalidate_backends_cache()


def _login_password_worker(name: str, username: str, password: str, code: str) -> None:
    """后台线程跑账号密码登录。

    安全：password 只作为局部变量存在，**不写日志、不入库、不回显**。
    `need_code` 用于把"等验证码"与"登录失败"区分开，前端据此显示输入框。
    """
    try:
        be = registry.get(name)
        if not hasattr(be, "login_with_password"):
            _login_finish(False, f"后端 {name} 不支持账号密码登录")
            return
        _login_event(f"已开始用账号 {username} 登录 X（浏览器窗口会打开）")
        try:
            ok, msg = be.login_with_password(
                username=username, password=password, code=code, on_event=_login_event)
        except TypeError:
            # 后端未接受 code 参数时的兼容路径
            ok, msg = be.login_with_password(
                username=username, password=password, on_event=_login_event)

        text = str(msg or ("登录成功" if ok else "登录失败"))
        with _login_lock:
            # 需要验证码：不是失败态，是"待补充输入"
            _login_state["need_code"] = (not ok) and ("验证码" in text)
        _login_finish(bool(ok), text)
    except Exception as e:
        log.exception("账号密码登录任务失败")
        # 异常信息里可能混入表单值，做一次兜底脱敏
        safe = _redact(str(e), password)
        _login_event(f"异常：{type(e).__name__}")
        _login_finish(False, f"登录异常：{type(e).__name__}: {safe}")
    finally:
        invalidate_backends_cache()


def _redact(text: str, *secrets_: str) -> str:
    """把敏感串从文本里抹掉（日志/返回值的最后一道防线）。"""
    out = text or ""
    for s in secrets_:
        if s and len(s) >= 3:
            out = out.replace(s, "***")
    return out


# ══════════════════════════════════════════════════════════
# 视图拼装
# ══════════════════════════════════════════════════════════

def settings_view() -> dict:
    raw = settings.all_settings()
    limit = settings.effective_monthly_limit()
    return {
        "backend": settings.current_backend(),
        "paused": settings.is_paused(),
        "tweet_prefix": raw.get("tweet_prefix", ""),
        "tweet_suffix": raw.get("tweet_suffix", ""),
        "quote_mode": raw.get("quote_mode", "auto"),
        "monthly_limit": limit,
        "dedup_window": settings.effective_dedup_window(),
        "max_attempts": config.MAX_ATTEMPTS,
        "mode": config.MODE,
        # 上次登录/识别到的 X 账号（只记账号名，绝不存密码）
        "x_username": raw.get("x_username", "") or "",
        "data_dir": str(config.DATA_DIR),
        "media_dir": str(config.MEDIA_DIR),
        "defaults": {
            "monthly_limit": config.MONTHLY_LIMIT,
            "dedup_window": config.DEDUP_WINDOW,
            "tweet_prefix": config.TWEET_PREFIX,
            "tweet_suffix": config.TWEET_SUFFIX,
        },
        # Telegram 相关（不含 token 明文）
        "tg": {
            "allowed_users": raw.get("tg_allowed_users", ""),
            "allowed_chats": raw.get("tg_allowed_chats", ""),
            "autostart": raw.get("tg_autostart", "0") == "1",
            "env_token_present": bool(getattr(config, "TG_TOKEN", "")),
        },
    }


def job_view(row: Any) -> dict:
    d = dict(row)
    body = d.get("tweet_text") or d.get("raw_text") or ""
    d["summary"] = (body[:80] + "…") if len(body) > 80 else body
    d["text_len"] = textutil.weighted_len(body)
    return d


def status_payload(limit_jobs: int | None = None) -> dict:
    st = queue.stats()
    # limit 用运行期生效值（用户可能在控制台改过），静态默认值另存一份备查
    st["limit"] = settings.effective_monthly_limit()
    st["limit_static"] = config.MONTHLY_LIMIT
    st["depth"] = queue.queue_depth()
    payload: dict[str, Any] = {
        "ok": True,
        "version": WEB_VERSION,
        "build": WEB_BUILD,
        "server_time": queue.now_iso(),
        "queue": st,
        "backend": settings.current_backend(),
        "backends": backends_view(),
        "paused": settings.is_paused(),
        "settings": settings_view(),
        "login": login_status(),
        "pipeline": pipeline_snapshot(),
        "tg": tg_status_view(),
        "media": media_limits_view(),
    }
    if limit_jobs:
        payload["jobs"] = [job_view(r) for r in queue.recent(limit=max(1, min(limit_jobs, 200)))]
    return payload


# ══════════════════════════════════════════════════════════
# pipeline 接入（可选依赖：core/pipeline.py 缺失时本服务照常工作）
# ══════════════════════════════════════════════════════════

_PIPE_LOCK = threading.Lock()
_pipe_obj: Any = None


def resolve_pipeline() -> tuple[Any | None, str]:
    """尽力拿到一个带 `publish_one(job_id)` 的 pipeline 实例。

    取用顺序（全部延迟导入 + try/except，**绝不 import bot.py**）：
      1. 统一启动器若把 Pipeline 实例注册进本模块（set_pipeline_instance）
      2. core.pipeline.Pipeline() 自建一个（用 LogNotifier，不碰 Telegram）
      3. 都没有 -> 返回 (None, 中文原因)，调用方回 503
    """
    global _pipe_obj
    with _PIPE_LOCK:
        if _pipe_obj is not None:
            return _pipe_obj, ""
        try:
            from core import pipeline as _pl  # 延迟导入：模块可能尚不存在
        except ImportError as e:
            return None, f"core/pipeline.py 缺失或导入失败（{e}）"
        cls = getattr(_pl, "Pipeline", None)
        if cls is None:
            return None, "core.pipeline 未提供 Pipeline 类"
        try:
            _pipe_obj = cls()
        except Exception as e:
            return None, f"Pipeline() 构造失败：{type(e).__name__}: {e}"
        return _pipe_obj, ""


def set_pipeline_instance(obj: Any) -> None:
    """供统一启动器（start.py）把正在跑的 Pipeline 实例交给控制台复用。"""
    global _pipe_obj
    with _PIPE_LOCK:
        _pipe_obj = obj


# ══════════════════════════════════════════════════════════
# Telegram 管理器接入（可选依赖：core/tgmanager.py 缺失时本服务照常工作）
# ══════════════════════════════════════════════════════════

_TG_LOCK = threading.Lock()
_tg_obj: Any = None


def resolve_tg() -> tuple[Any | None, str]:
    """拿到 TelegramManager。顺序：start.py 注入 → 自建 → (None, 原因)。

    **绝不 import bot.py**（那会拉起长轮询）；tgmanager 内部才延迟导入。
    """
    global _tg_obj
    with _TG_LOCK:
        if _tg_obj is not None:
            return _tg_obj, ""
        try:
            from core.tgmanager import TelegramManager  # 延迟导入
        except ImportError as e:
            return None, f"core/tgmanager.py 缺失或导入失败（{e}）"
        except Exception as e:
            return None, f"Telegram 模块加载失败：{type(e).__name__}: {e}"
        try:
            _tg_obj = TelegramManager()
        except Exception as e:
            return None, f"TelegramManager() 构造失败：{type(e).__name__}: {e}"
        return _tg_obj, ""


def set_tg_manager_instance(obj: Any) -> None:
    """供统一启动器（start.py）把正在跑的 TelegramManager 交给控制台复用。

    必须复用同一个实例 —— 否则控制台「停止机器人」停不掉启动器起的那个轮询。
    """
    global _tg_obj
    with _TG_LOCK:
        _tg_obj = obj


def tg_status_view() -> dict:
    """机器人状态；模块缺失时返回可读的不可用状态而非报错。"""
    tg, why = resolve_tg()
    if tg is None:
        return {"available": False, "running": False, "reason": why,
                "token_set": False, "token_masked": "", "bot_username": ""}
    try:
        st = dict(tg.status() or {})
    except Exception as e:
        log.exception("tg.status() 失败")
        return {"available": False, "running": False,
                "reason": f"状态查询失败：{type(e).__name__}: {e}",
                "token_set": False, "token_masked": "", "bot_username": ""}
    st["available"] = True
    return st


def pipeline_snapshot() -> dict:
    pipe, why = resolve_pipeline()
    if pipe is None:
        return {"ready": False, "reason": why}
    snap: dict[str, Any] = {"ready": True}
    fn = getattr(pipe, "snapshot", None)
    if callable(fn):
        try:
            snap.update(_jsonable(fn()))
        except Exception as e:
            snap["reason"] = f"snapshot() 失败：{type(e).__name__}: {e}"
    return snap


# ══════════════════════════════════════════════════════════
# 认证 / 异常处理
# ══════════════════════════════════════════════════════════


_LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>twitbot 控制台 · 登录</title>
<style>
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:#0f1419;color:#e7e9ea;font:15px/1.5 -apple-system,BlinkMacSystemFont,
       "Segoe UI","Microsoft YaHei",sans-serif}
  .box{width:100%;max-width:360px;padding:28px;background:#16181c;border:1px solid #2f3336;
       border-radius:16px}
  h1{margin:0 0 6px;font-size:20px}
  p.sub{margin:0 0 20px;color:#71767b;font-size:13px}
  input{width:100%;padding:12px 14px;background:#202327;border:1px solid #2f3336;
        border-radius:10px;color:#e7e9ea;font-size:15px;outline:none}
  input:focus{border-color:#1d9bf0}
  button{width:100%;margin-top:14px;padding:12px;background:#1d9bf0;border:0;border-radius:10px;
         color:#fff;font-size:15px;font-weight:600;cursor:pointer}
  .err{margin-top:12px;padding:10px 12px;background:#3a1a1c;border:1px solid #6b2429;
       border-radius:8px;color:#ff8b8b;font-size:13px;display:none}
  .err.show{display:block}
</style></head>
<body>
  <form class="box" id="f">
    <h1>twitbot 控制台</h1>
    <p class="sub">请输入管理密码</p>
    <input type="password" id="pw" autocomplete="current-password"
           placeholder="管理密码" autofocus required>
    <button type="submit" id="btn">进入</button>
    <div class="err" id="err"></div>
  </form>
<script>
  var f=document.getElementById('f'),pw=document.getElementById('pw'),
      btn=document.getElementById('btn'),err=document.getElementById('err');
  f.addEventListener('submit',async function(e){
    e.preventDefault(); btn.disabled=true; err.classList.remove('show');
    try{
      var r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({password:pw.value})});
      if(r.ok){location.href='/';return}
      var j=await r.json().catch(function(){return{}});
      err.textContent=j.error||'密码不正确'; err.classList.add('show');
    }catch(ex){err.textContent='网络错误：'+ex.message; err.classList.add('show')}
    btn.disabled=false;
  });
</script></body></html>"""


def expected_token() -> str:
    """运行期读取（测试可 monkeypatch core.config.WEB_TOKEN）。"""
    return (getattr(config, "WEB_TOKEN", "") or "").strip()


# ══════════════════════════════════════════════════════════
# 控制台密码门（服务器版专属）
# ══════════════════════════════════════════════════════════
# 默认控制台密码**不写死**：首次启动随机生成并记在 data/.console-password，
# 避免所有部署共用同一个密码（那等于没密码）。用户可用 WEB_PASSWORD 覆盖。
WEB_PASSWORD_FILE = "console-password"
_PW_LOCK = threading.Lock()


def _load_or_create_password() -> str:
    """读或创建控制台密码文件。返回明文密码（仅用于比对）。"""
    try:
        from core import config as _cfg
        f = Path(_cfg.DATA_DIR) / WEB_PASSWORD_FILE
        with _PW_LOCK:
            if f.exists():
                v = f.read_text(encoding="utf-8").strip()
                if v:
                    return v
            v = secrets.token_urlsafe(12)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(v, encoding="utf-8")
            try:
                os.chmod(f, 0o600)
            except Exception:
                pass
            return v
    except Exception:
        return ""
SESSION_COOKIE = "twitbot_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
_sessions: dict[str, float] = {}
_sessions_lock = threading.Lock()


def console_password() -> str:
    """控制台密码。WEB_PASSWORD 非空用它，否则用默认；设为 "-" 表示关闭。"""
    env = (os.getenv("WEB_PASSWORD") or "").strip()
    if env == "-":
        return ""
    if env:
        return env
    return _load_or_create_password()


def _new_session() -> str:
    tok = secrets.token_urlsafe(32)
    with _sessions_lock:
        now = time.time()
        for k in [k for k, exp in _sessions.items() if exp < now]:
            _sessions.pop(k, None)
        _sessions[tok] = now + SESSION_TTL_SECONDS
    return tok


def _session_valid(tok: str) -> bool:
    if not tok:
        return False
    with _sessions_lock:
        exp = _sessions.get(tok, 0)
        if exp and exp < time.time():
            _sessions.pop(tok, None)
            return False
        return bool(exp)


def _session_cookie_ok(request: Request) -> bool:
    try:
        return _session_valid(request.cookies.get(SESSION_COOKIE, "") or "")
    except Exception:
        return False


def _extract_token(request: Request) -> str:
    auth = request.headers.get("authorization", "") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.query_params.get("token") or "").strip()


# ══════════════════════════════════════════════════════════
# 请求体模型
# ══════════════════════════════════════════════════════════

class JobIn(BaseModel):
    text: str | None = ""
    media_path: str | None = ""
    quote_id: str | None = ""
    kind: str | None = ""
    status: str | None = "pending"


class BackendIn(BaseModel):
    backend: str | None = ""


class PauseIn(BaseModel):
    paused: bool | int | str | None = None


class SettingsIn(BaseModel):
    tweet_prefix: str | None = None
    tweet_suffix: str | None = None
    monthly_limit: int | str | None = None
    dedup_window: int | str | None = None
    quote_mode: str | None = None
    # 也允许整体覆盖（等价于 /api/pause）
    paused: bool | int | str | None = None
    # Telegram 白名单与自启（token 走 /api/tg/* 专用接口，不在这里）
    tg_allowed_users: str | None = None
    tg_allowed_chats: str | None = None
    tg_autostart: bool | int | str | None = None
    x_username: str | None = None


class TgTokenIn(BaseModel):
    """只校验 token，不启动。"""
    token: str | None = ""


class TgStartIn(BaseModel):
    """启动/重启机器人。token 为空则用已保存的（settings → .env）。"""
    token: str | None = ""
    allowed_users: str | None = None
    allowed_chats: str | None = None


class MediaValidateIn(BaseModel):
    media: str | None = ""
    kind: str | None = ""


class PasswordLoginIn(BaseModel):
    """账号密码登录。password 只在本次请求内存中活一次，绝不落库。"""
    username: str | None = ""
    password: str | None = ""
    code: str | None = ""      # 二次验证码（可选）


def _as_bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "on", "是", "开"):
            return True
        if s in ("0", "false", "no", "off", "", "否", "关"):
            return False
    return default


def _as_int(v: Any, field: str, lo: int = 0, hi: int = 1_000_000) -> int:
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        raise HTTPException(400, f"{field} 必须是整数")
    if not (lo <= n <= hi):
        raise HTTPException(400, f"{field} 超出范围（{lo}~{hi}）")
    return n


def resolve_media_path(media: str) -> Path | None:
    """把媒体名解析成 MEDIA_DIR 下的绝对路径；越界（含穿越）返回 None。

    与 `_resolve_media` 的区别：这里**只接受** MEDIA_DIR 内的文件，
    用于上传/校验接口，杜绝借绝对路径读取任意文件。
    """
    s = (media or "").strip().strip('"').replace("\\", "/")
    if not s:
        return None
    name = Path(s).name           # 只取 basename，天然挡掉 ../ 与绝对路径
    if not name or name in (".", ".."):
        return None
    cand = (config.MEDIA_DIR / name).resolve()
    try:
        cand.relative_to(config.MEDIA_DIR.resolve())
    except ValueError:
        return None
    return cand


def media_limits_view() -> dict:
    """把 core.media 的上限常量暴露给前端展示（模块缺失时给静态兜底）。"""
    try:
        from core import media as m
        return {
            "image_max_mb": round(m.IMAGE_MAX_BYTES / 1024 / 1024, 1),
            "gif_max_mb": round(m.GIF_MAX_BYTES / 1024 / 1024, 1),
            "video_max_mb": round(m.VIDEO_MAX_BYTES / 1024 / 1024, 1),
            "video_max_seconds": m.VIDEO_MAX_SECONDS,
            "max_images": m.MAX_IMAGES,
            "kinds": sorted(set(getattr(m, "KIND_BY_EXT", {}).values())) or
                     ["photo", "video", "document"],
        }
    except Exception:
        return {"image_max_mb": 5.0, "gif_max_mb": 15.0, "video_max_mb": 512.0,
                "video_max_seconds": 140, "max_images": 4,
                "kinds": ["photo", "video", "document"]}


def tg_settings_view() -> dict:
    """Telegram 相关的可配置项（**不回显 token 明文**）。"""
    raw = settings.all_settings()
    return {
        "allowed_users": raw.get("tg_allowed_users", ""),
        "allowed_chats": raw.get("tg_allowed_chats", ""),
        "autostart": raw.get("tg_autostart", "0") == "1",
        "env_token_present": bool(getattr(config, "TG_TOKEN", "")),
    }


def _validate_media_or_pass(path: Path, kind: str) -> tuple[bool, str]:
    """跑 core.media.validate()；模块不存在时放行（保持向后兼容）。"""
    try:
        from core import media as m
    except Exception:
        return True, ""
    try:
        ok, why = m.validate(path, kind or "")
        return bool(ok), str(why or "")
    except Exception as e:
        log.warning("媒体校验异常（放行）：%s", e)
        return True, ""


def _guess_kind(media_path: str) -> str:
    if not media_path:
        return "text"
    # 优先用 core.media 的判定（扩展名+mime，集中一份真相），
    # 模块缺失时回落到本文件的静态映射表。
    try:
        from core import media as m
        return m.detect_kind(media_path) or "document"
    except Exception:
        return MEDIA_EXT_KIND.get(Path(media_path).suffix.lower(), "document")


def _resolve_media(media_path: str) -> tuple[str, Path | None]:
    """校验并归一媒体路径。返回 (入库值, 绝对路径|None)。"""
    media_path = (media_path or "").strip().strip('"')
    if not media_path:
        return "", None
    p = Path(media_path)
    cand = p if p.is_absolute() else (config.MEDIA_DIR / p)
    if not cand.is_file():
        raise HTTPException(400, f"媒体文件不存在：{cand}（相对路径按 DATA_DIR/media 解析）")
    # 库内存相对名（与 bot.py 一致），便于目录整体搬迁
    try:
        rel = cand.resolve().relative_to(config.MEDIA_DIR.resolve()).as_posix()
        return rel, cand
    except ValueError:
        return str(cand.resolve()), cand


# ══════════════════════════════════════════════════════════
# 应用工厂
# ══════════════════════════════════════════════════════════

def create_app() -> FastAPI:
    """构建 FastAPI 应用（不改全局状态；start.py 可同进程挂载）。"""
    # 表初始化：jobs + settings（settings 表由 core.settings 惰性建，这里主动触发一次）
    queue.init_db()
    try:
        settings.all_settings()
    except Exception:
        log.exception("settings 表初始化失败（不致命）")

    app = FastAPI(
        title="twitbot 控制台",
        description="X 发布队列与后端管理（本地服务）",
        version=WEB_VERSION,
        build=WEB_BUILD,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    # ── 异常：统一 {"error": "中文"} ──────────────────────
    @app.exception_handler(HTTPException)
    async def _http_err(_req: Request, exc: HTTPException):
        return JSONResponse(
            {"error": str(exc.detail), "detail": str(exc.detail)},
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_err(_req: Request, exc: RequestValidationError):
        try:
            errs = exc.errors()
            types = {str(e.get("type", "")) for e in errs}
            # 请求体不是合法 JSON —— 对调用方来说这是 400 而不是 422
            if types & {"json_invalid"} or any("JSON decode error" in str(e.get("msg", ""))
                                               for e in errs):
                return JSONResponse({"error": "请求体不是合法的 JSON"}, status_code=400)
            first = errs[0]
            where = ".".join(str(x) for x in first.get("loc", ()) if x != "body") or "body"
            msg = f"请求参数有误：{where} {first.get('msg', '')}".strip()
        except Exception:
            msg = "请求参数有误"
        return JSONResponse({"error": msg}, status_code=422)

    @app.exception_handler(Exception)
    async def _any_err(_req: Request, exc: Exception):
        log.exception("未捕获异常")
        return JSONResponse({"error": f"服务内部错误：{type(exc).__name__}: {exc}"}, status_code=500)

    # ── 认证中间件（覆盖 / 与静态资源）──────────────────
    @app.middleware("http")
    async def _auth(request: Request, call_next):
        path = request.url.path

        # ── 第一道：密码门（服务器版）──
        pw = console_password()
        if pw and path not in ("/login", "/healthz", "/favicon.ico"):
            if not _session_cookie_ok(request):
                wants_html = ("text/html" in (request.headers.get("accept", "") or ""))
                if wants_html:
                    return HTMLResponse(_LOGIN_HTML, status_code=200)
                return JSONResponse({"error": "未登录：请先在控制台完成密码登录"},
                                    status_code=401)

        # ── 第二道：原有的 ?token= / Bearer ──
        want = expected_token()
        if want:
            got = _extract_token(request)
            if not got or not secrets.compare_digest(got, want):
                return JSONResponse(
                    {"error": "未授权：缺少或错误的访问 token（?token= 或 Authorization: Bearer）"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
        resp = await call_next(request)
        if path in ("/", "/index.html"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.post("/login", include_in_schema=False)
    async def do_login(body: dict):
        pw = console_password()
        if not pw:
            return JSONResponse({"ok": True, "message": "未启用密码门"})
        got = str((body or {}).get("password") or "")
        if not got or not secrets.compare_digest(got, pw):
            return JSONResponse({"error": "密码不正确"}, status_code=401)
        tok = _new_session()
        resp = JSONResponse({"ok": True})
        resp.set_cookie(SESSION_COOKIE, tok, max_age=SESSION_TTL_SECONDS,
                        httponly=True, samesite="lax", path="/")
        return resp

    @app.post("/logout", include_in_schema=False)
    async def do_logout(request: Request):
        tok = request.cookies.get(SESSION_COOKIE, "")
        if tok:
            with _sessions_lock:
                _sessions.pop(tok, None)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    # ── 前端 ─────────────────────────────────────────────
    @app.get("/", include_in_schema=False)
    async def index():
        if not INDEX_FILE.is_file():
            raise HTTPException(500, f"前端文件缺失：{INDEX_FILE}")
        return FileResponse(INDEX_FILE, media_type="text/html; charset=utf-8")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return Response(status_code=204)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True, "version": WEB_VERSION}

    # ── 状态 ─────────────────────────────────────────────
    @app.get("/api/status")
    async def api_status(with_jobs: int = 0):
        return await asyncio.to_thread(status_payload, with_jobs)

    # ── 任务 ─────────────────────────────────────────────
    @app.get("/api/jobs")
    async def api_jobs(limit: int = 50, status: str = ""):
        limit = max(1, min(int(limit or 50), 500))
        st = (status or "").strip()
        if st and st not in VALID_STATUS:
            raise HTTPException(400, f"未知状态 {st!r}；可选：{'/'.join(VALID_STATUS)}")
        rows = await asyncio.to_thread(queue.recent, limit, st or None)
        items = [job_view(r) for r in rows]
        return {"jobs": items, "items": items, "count": len(items),
                "limit": limit, "status": st}

    @app.post("/api/jobs")
    async def api_jobs_create(body: JobIn):
        text = (body.text or "").strip()
        media_rel, media_abs = _resolve_media(body.media_path or "")
        kind = (body.kind or "").strip().lower() or _guess_kind(media_rel)
        if kind not in VALID_KINDS:
            raise HTTPException(400, f"未知类型 {kind!r}；可选：{'/'.join(VALID_KINDS)}")
        if not text and not media_rel:
            raise HTTPException(400, "正文与媒体至少填一项")
        if kind == "text" and media_rel:
            kind = _guess_kind(media_rel)

        # 媒体合规校验（超过 X 平台限制就当场拒绝，别等发布时才失败）
        if media_abs is not None:
            v_ok, v_why = _validate_media_or_pass(media_abs, kind)
            if not v_ok:
                raise HTTPException(400, v_why)

        status = (body.status or "pending").strip().lower() or "pending"
        if status not in ("pending", "awaiting"):
            raise HTTPException(400, "新建任务的状态只能是 pending（立即排队）或 awaiting（待人工确认）")

        quote_id = (body.quote_id or "").strip()
        if not quote_id:
            quote_id = textutil.first_tweet_id(text) or ""
        if quote_id and not quote_id.isdigit():
            raise HTTPException(400, "引用推文 ID 必须是数字（也可直接粘贴推文链接）")
        if not quote_id:
            m = textutil.TWEET_ID_RE.search(text or "")
            quote_id = m.group(1) if m else ""

        c_hash = textutil.content_hash(kind, text, quote_id,
                                      media_abs.name if media_abs else "")
        window = settings.effective_dedup_window()
        if await asyncio.to_thread(queue.is_dup_content, c_hash, window):
            raise HTTPException(
                409,
                f"重复内容：{window} 秒内（去重窗口可改）已有相同正文/媒体在队列中或已发出",
            )

        job_id = await asyncio.to_thread(
            queue.enqueue, kind=kind, tg_chat_id=0, tg_msg_id=0, raw_text=text,
            media_path=media_rel, quote_id=quote_id, content_hash=c_hash, status=status,
        )
        if job_id is None:
            raise HTTPException(409, "入队失败：内容重复或队列唯一键冲突")
        row = await asyncio.to_thread(queue.get, job_id)
        preview = textutil.compose(settings.effective_prefix(), text,
                                   settings.effective_suffix())
        return {
            "ok": True, "id": job_id, "job": job_view(row) if row else None,
            "status": status, "kind": kind, "backend": settings.current_backend(),
            "preview": preview, "weighted_len": textutil.weighted_len(preview),
            "dedup_window": window,
        }

    @app.post("/api/jobs/{job_id}/requeue")
    async def api_jobs_requeue(job_id: int):
        row = await asyncio.to_thread(queue.get, job_id)
        if row is None:
            raise HTTPException(404, f"任务 #{job_id} 不存在")
        ok = await asyncio.to_thread(queue.requeue, job_id)
        if not ok:
            raise HTTPException(
                409, f"任务 #{job_id} 当前状态为 {row['status']}，不可重发（仅 failed/canceled 可重发）")
        new_row = await asyncio.to_thread(queue.get, job_id)
        return {"ok": True, "id": job_id, "job": job_view(new_row),
                "message": "已重新入队（待发），发布循环会自行取走"}

    @app.post("/api/jobs/{job_id}/cancel")
    async def api_jobs_cancel(job_id: int):
        row = await asyncio.to_thread(queue.get, job_id)
        if row is None:
            raise HTTPException(404, f"任务 #{job_id} 不存在")
        ok = await asyncio.to_thread(queue.cancel, job_id)
        if not ok:
            raise HTTPException(
                409, f"任务 #{job_id} 当前状态为 {row['status']}，不可取消（仅 pending/awaiting 可取消）")
        new_row = await asyncio.to_thread(queue.get, job_id)
        return {"ok": True, "id": job_id, "job": job_view(new_row), "message": "已取消"}

    @app.get("/api/jobs/{job_id}")
    async def api_job_get(job_id: int):
        row = await asyncio.to_thread(queue.get, job_id)
        if row is None:
            raise HTTPException(404, f"任务 #{job_id} 不存在")
        return {"job": job_view(row)}

    @app.post("/api/jobs/{job_id}/publish")
    async def api_jobs_publish(job_id: int):
        """立即发送一条（依赖 core.pipeline；缺失则 503，不影响本服务其它功能）。"""
        row = await asyncio.to_thread(queue.get, job_id)
        if row is None:
            raise HTTPException(404, f"任务 #{job_id} 不存在")
        pipe, why = resolve_pipeline()
        if pipe is None:
            raise HTTPException(503, f"发布模块未就绪：{why}")
        try:
            result = await pipe.publish_one(job_id)
        except HTTPException:
            raise
        except Exception as e:
            log.exception("立即发送失败")
            raise HTTPException(500, f"立即发送失败：{type(e).__name__}: {e}") from e
        new_row = await asyncio.to_thread(queue.get, job_id)
        res = _jsonable(result)
        # 外层 ok 表示"请求已处理"，不代表发布成功；用 published 明确表达发布结果，
        # 避免调用方误以为 ok:true 就是发出去了。task_status 便于前端展示。
        return {
            "ok": True,
            "published": bool(res.get("ok")) if isinstance(res, dict) else False,
            "id": job_id,
            "result": res,
            "job": job_view(new_row) if new_row else None,
        }

    # ── 后端 ─────────────────────────────────────────────
    @app.get("/api/backends")
    async def api_backends():
        items = await asyncio.to_thread(backends_view, True)
        return {"backends": items, "current": settings.current_backend()}

    @app.post("/api/backend")
    async def api_backend_switch(body: BackendIn):
        name = (body.backend or "").strip().lower()
        if not name:
            raise HTTPException(400, "缺少 backend 参数")
        if name not in registry.REGISTRY:
            raise HTTPException(400, f"未知后端 {name!r}；可选：{'/'.join(registry.REGISTRY)}")
        # 重点：切换前必须校验目标后端可用，否则任务会整体卡死
        _be, ok, reason = await asyncio.to_thread(backend_available_or_400, name)
        if not ok:
            raise HTTPException(400, f"后端「{name}」当前不可用，未切换：{reason or '原因未知'}")
        await asyncio.to_thread(settings.set_many, {"backend": name})
        invalidate_backends_cache()
        return {"ok": True, "backend": name,
                "label": registry.REGISTRY[name][2], "message": f"已切换到「{registry.REGISTRY[name][2]}」"}

    @app.post("/api/backends/{name}/verify")
    async def api_backend_verify(name: str):
        be = get_backend(name)
        try:
            ok, reason = await asyncio.to_thread(be.verify)
        except Exception as e:
            ok, reason = False, f"verify() 异常：{type(e).__name__}: {e}"
        invalidate_backends_cache()
        return {"ok": bool(ok), "backend": name, "message": str(reason or ""),
                "available": bool(ok)}

    @app.post("/api/backends/{name}/login")
    async def api_backend_login(name: str):
        be = get_backend(name)  # 模块缺失 → 400（含原因），前端优雅显示
        if not hasattr(be, "login"):
            raise HTTPException(400, f"后端 {name} 未实现 login()")
        if login_status().get("running"):
            raise HTTPException(409, "登录已在进行中，请等待当前登录结束（或稍后查看进度）")
        _login_reset(name)
        threading.Thread(target=_login_worker, args=(name,), name=f"login-{name}",
                         daemon=True).start()
        return {
            "ok": True, "started": True, "backend": name,
            "purpose": "浏览器窗口会打开，请在其中完成登录",
            "status_url": f"/api/backends/{name}/login/status",
            "message": "登录流程已在后台启动，请在弹出的浏览器窗口完成登录",
        }

    @app.post("/api/backends/{name}/login-chrome")
    async def api_backend_login_chrome(name: str):
        """用真实 Chrome 登录（推荐）。

        与 /login 的区别：/login 走 Playwright 启动浏览器（带自动化指纹，
        容易被 x.com 风控拦截）；本接口用普通方式启动系统 Chrome，
        不会被判定为自动化客户端。
        """
        from core import chrome_login  # 延迟导入：便于给出可读的缺依赖提示

        if not chrome_login.find_chrome():
            raise HTTPException(
                400, "找不到 Chrome。请安装 Google Chrome，"
                     "或用环境变量 CHROME_PATH 指定 chrome.exe 的完整路径。")
        be = get_backend(name)
        if not hasattr(be, "state_path"):
            raise HTTPException(400, f"后端 {name} 没有 state_path()，无法保存登录态")
        if login_status().get("running"):
            raise HTTPException(409, "登录已在进行中，请等待当前登录结束（或稍后查看进度）")
        _login_reset(name)
        threading.Thread(target=_login_chrome_worker, args=(name,),
                         name=f"login-chrome-{name}", daemon=True).start()
        return {
            "ok": True, "started": True, "backend": name, "mode": "chrome",
            "purpose": "Chrome 窗口会打开，请在那里正常登录 X",
            "status_url": f"/api/backends/{name}/login/status",
            "message": "真实 Chrome 登录已启动，请在弹出的窗口完成登录",
        }

    @app.get("/api/backends/{name}/login/status")
    async def api_backend_login_status(name: str):
        snap = login_status()
        snap["ok_requested"] = True
        snap["name"] = name
        return snap

    # ── Telegram 机器人（启停 / 配置 / 校验）───────────────
    @app.get("/api/tg/status")
    async def api_tg_status():
        return {"ok": True, "tg": tg_status_view(), "settings": tg_settings_view()}

    @app.post("/api/tg/verify")
    async def api_tg_verify(body: TgTokenIn):
        """只校验 token，不启动轮询。"""
        tg, why = resolve_tg()
        if tg is None:
            raise HTTPException(503, f"Telegram 模块不可用：{why}")
        token = (body.token or "").strip()
        if not token:
            raise HTTPException(400, "请先填写 bot token")
        try:
            ok, msg, info = await tg.verify_token(token)
        except Exception as e:
            log.exception("tg.verify_token 异常")
            raise HTTPException(500, f"校验失败：{type(e).__name__}: {e}") from e
        return {"ok": bool(ok), "message": msg, "info": _jsonable(info)}

    @app.post("/api/tg/start")
    async def api_tg_start(body: TgStartIn):
        tg, why = resolve_tg()
        if tg is None:
            raise HTTPException(503, f"Telegram 模块不可用：{why}")
        token = (body.token or "").strip() or None
        try:
            ok, msg = await tg.start(
                token=token,
                allowed_users=body.allowed_users,
                allowed_chats=body.allowed_chats,
            )
        except Exception as e:
            log.exception("tg.start 异常")
            raise HTTPException(500, f"启动失败：{type(e).__name__}: {e}") from e
        if not ok:
            # 400 而非 500：多为 token 没填/无效，属于用户可修正的输入问题
            raise HTTPException(400, msg or "启动失败")
        return {"ok": True, "message": msg, "tg": tg_status_view()}

    @app.post("/api/tg/stop")
    async def api_tg_stop():
        tg, why = resolve_tg()
        if tg is None:
            raise HTTPException(503, f"Telegram 模块不可用：{why}")
        try:
            ok, msg = await tg.stop()
        except Exception as e:
            log.exception("tg.stop 异常")
            raise HTTPException(500, f"停止失败：{type(e).__name__}: {e}") from e
        if not ok:
            raise HTTPException(400, msg or "停止失败")
        return {"ok": True, "message": msg, "tg": tg_status_view()}

    @app.post("/api/tg/restart")
    async def api_tg_restart(body: TgStartIn):
        tg, why = resolve_tg()
        if tg is None:
            raise HTTPException(503, f"Telegram 模块不可用：{why}")
        token = (body.token or "").strip() or None
        try:
            ok, msg = await tg.restart(
                token=token,
                allowed_users=body.allowed_users,
                allowed_chats=body.allowed_chats,
            )
        except Exception as e:
            log.exception("tg.restart 异常")
            raise HTTPException(500, f"重启失败：{type(e).__name__}: {e}") from e
        if not ok:
            raise HTTPException(400, msg or "重启失败")
        return {"ok": True, "message": msg, "tg": tg_status_view()}

    # ── 浏览器登录态：上传 / 查看（服务器部署的关键）──────
    @app.post("/api/browser/state/upload")
    async def api_browser_state_upload(file: UploadFile = File(...)):
        """上传 storage_state.json。只接受合法 JSON 且必须含 auth_token。"""
        import json as _json
        from core.backends import browser as _browser_mod

        raw = await file.read()
        if not raw:
            raise HTTPException(400, "上传内容为空")
        if len(raw) > 4 * 1024 * 1024:
            raise HTTPException(400, "文件过大（登录态通常几十 KB）")
        try:
            data = _json.loads(raw.decode("utf-8", errors="replace"))
        except Exception as e:
            raise HTTPException(400, f"不是合法 JSON：{type(e).__name__}: {e}")
        if not isinstance(data, dict) or "cookies" not in data:
            raise HTTPException(400, "格式不对：应是 Playwright 的 storage_state.json（需含 cookies）")
        cookies = data.get("cookies") or []
        if not any((c or {}).get("name") == "auth_token" for c in cookies if isinstance(c, dict)):
            raise HTTPException(400, "这份登录态里没有 auth_token —— 可能是没登录成功就导出了")
        try:
            path = _browser_mod.BrowserBackend().state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass
        except Exception as e:
            raise HTTPException(500, f"写入失败：{type(e).__name__}: {e}")
        invalidate_backends_cache()
        n = len([c for c in cookies if isinstance(c, dict)])
        return {"ok": True, "message": f"登录态已保存（{n} 条 cookie）", "path": str(path)}

    @app.get("/api/browser/state")
    async def api_browser_state():
        """看登录态是否存在（**不回传 cookie 内容**）。"""
        from core.backends import browser as _browser_mod
        import json as _json
        be = _browser_mod.BrowserBackend()
        path = be.state_path()
        if not path.exists():
            return {"ok": True, "exists": False, "message": "尚未上传登录态"}
        try:
            data = _json.loads(path.read_text(encoding="utf-8"))
            names = sorted({(c or {}).get("name", "") for c in (data.get("cookies") or [])
                            if isinstance(c, dict) and (c or {}).get("name")})
            return {"ok": True, "exists": True, "cookie_names": names,
                    "has_auth": "auth_token" in names,
                    "account": be.saved_account(),
                    "size": path.stat().st_size}
        except Exception as e:
            return {"ok": True, "exists": True, "broken": True,
                    "message": f"文件存在但读不出来：{type(e).__name__}: {e}"}

    # ── 媒体：上传 / 列表 / 删除 ──────────────────────────
    @app.post("/api/media/upload")
    async def api_media_upload(file: UploadFile = File(...)):
        """上传图片/视频，落 MEDIA_DIR，返回可拿去投料的相对名。"""
        try:
            from core import media as media_mod
        except ImportError as e:
            raise HTTPException(503, f"core/media.py 不可用（{e}）") from e

        raw = await file.read()
        if not raw:
            raise HTTPException(400, "上传内容为空")
        # 先按上限粗筛，避免超大文件把内存/磁盘打爆
        hard_cap = 512 * 1024 * 1024
        if len(raw) > hard_cap:
            raise HTTPException(400, "文件过大（超过 512MB）")

        name = file.filename or "upload.bin"
        ok, err, rel = await asyncio.to_thread(
            media_mod.save_upload, name, raw, config.MEDIA_DIR)
        if not ok:
            raise HTTPException(400, err or "保存失败")

        info = await asyncio.to_thread(media_mod.summarize, config.MEDIA_DIR / rel)
        info["rel"] = rel
        if not info.get("ok", True):
            # 存下来了但不符合 X 限制 → 明确告知，且不删除文件（用户可换一个）
            return JSONResponse(status_code=400, content={
                "ok": False, "error": info.get("reason") or "文件不符合平台限制",
                "media": _jsonable(info), "rel": rel,
            })
        return {"ok": True, "media": _jsonable(info), "rel": rel,
                "message": f"已上传：{info.get('name')}（{info.get('human_size')}）"}

    @app.get("/api/media/list")
    async def api_media_list(limit: int = 50):
        try:
            from core import media as media_mod
        except ImportError as e:
            raise HTTPException(503, f"core/media.py 不可用（{e}）") from e
        n = max(1, min(int(limit or 50), 500))
        d = config.MEDIA_DIR

        def _scan() -> list[dict]:
            items: list[dict] = []
            if not d.exists():
                return items
            files = sorted(
                (p for p in d.iterdir() if p.is_file()),
                key=lambda p: p.stat().st_mtime, reverse=True)[:n]
            for p in files:
                try:
                    items.append(media_mod.summarize(p))
                except Exception as e:
                    items.append({"name": p.name, "ok": False,
                                  "reason": f"读取失败：{type(e).__name__}"})
            return items

        items = await asyncio.to_thread(_scan)
        return {"ok": True, "media": _jsonable(items), "dir": str(d),
                "limits": media_limits_view()}

    @app.get("/api/media/file")
    async def api_media_file(name: str = ""):
        """回传 MEDIA_DIR 内的媒体文件（控制台缩略图预览用）。

        只接受 baseName，且 resolve 后必须仍在 MEDIA_DIR 内 —— 挡目录穿越。
        """
        p = resolve_media_path(name)
        if p is None:
            raise HTTPException(400, "文件名非法")
        if not p.is_file():
            raise HTTPException(404, "文件不存在")
        return FileResponse(p)

    @app.post("/api/media/validate")
    async def api_media_validate(body: MediaValidateIn):
        try:
            from core import media as media_mod
        except ImportError as e:
            raise HTTPException(503, f"core/media.py 不可用（{e}）") from e
        p = resolve_media_path(body.media or "")
        if p is None:
            raise HTTPException(400, "媒体路径非法（只接受 MEDIA_DIR 下的文件名）")
        if not p.exists():
            raise HTTPException(404, "媒体文件不存在")
        ok, why = await asyncio.to_thread(media_mod.validate, p, body.kind or "")
        return {"ok": bool(ok), "message": why, "media": _jsonable(
            await asyncio.to_thread(media_mod.summarize, p))}

    # ── 浏览器后端的账号密码登录 ──────────────────────────
    @app.post("/api/backends/{name}/login-password")
    async def api_backend_login_password(name: str, body: PasswordLoginIn):
        """用账号密码登录 X（浏览器后端）。密码只在本请求内存中活一次，绝不落库。"""
        be = get_backend(name)
        if not hasattr(be, "login_with_password"):
            raise HTTPException(
                400, f"后端 {name} 不支持账号密码登录（请用『登录 X』打开窗口手工登录）")
        if login_status().get("running"):
            raise HTTPException(409, "已有登录流程在进行中，请等它结束")
        username = (body.username or "").strip()
        password = body.password or ""
        if not username or not password:
            raise HTTPException(400, "账号与密码都要填")

        _login_reset(name)
        # 注意：password 只作为参数传入线程，不写日志、不入库、不回显。
        threading.Thread(
            target=_login_password_worker,
            args=(name, username, password, (body.code or "").strip()),
            name=f"login-pw-{name}", daemon=True,
        ).start()
        return {
            "ok": True, "started": True, "backend": name,
            "username": username,
            "status_url": f"/api/backends/{name}/login/status",
            "message": ("已开始账号密码登录（后台进行）。若 X 要求验证码，"
                        "流程会停下并在此提示你填入。"),
        }

    # ── 暂停 / 设置 ──────────────────────────────────────
    @app.post("/api/pause")
    async def api_pause(body: PauseIn):
        paused = _as_bool(body.paused, settings.is_paused())
        await asyncio.to_thread(settings.set_many, {"paused": "1" if paused else "0"})
        return {"ok": True, "paused": paused, "message": "已暂停出队" if paused else "已恢复出队"}

    @app.post("/api/settings")
    async def api_settings(body: SettingsIn):
        items: dict[str, str] = {}
        if body.tweet_prefix is not None:
            v = str(body.tweet_prefix)
            if len(v) > 140:
                raise HTTPException(400, "前缀过长（≤140 字符）")
            items["tweet_prefix"] = v
        if body.tweet_suffix is not None:
            v = str(body.tweet_suffix)
            if len(v) > 140:
                raise HTTPException(400, "后缀过长（≤140 字符）")
            items["tweet_suffix"] = v
        if body.monthly_limit is not None:
            items["monthly_limit"] = str(_as_int(body.monthly_limit, "monthly_limit", 0, 100_000))
        if body.dedup_window is not None:
            items["dedup_window"] = str(_as_int(body.dedup_window, "dedup_window", 0, 86400 * 30))
        if body.quote_mode is not None:
            qm = str(body.quote_mode).strip().lower()
            if qm not in ("auto", "off"):
                raise HTTPException(400, "quote_mode 只能是 auto 或 off")
            items["quote_mode"] = qm
        if body.paused is not None:
            items["paused"] = "1" if _as_bool(body.paused, False) else "0"
        if body.tg_allowed_users is not None:
            v = str(body.tg_allowed_users).strip()
            if len(v) > 500:
                raise HTTPException(400, "tg_allowed_users 过长（≤500 字符）")
            # 只允许数字、逗号、空白与 - 号，防止塞进奇怪内容
            if v and not re.fullmatch(r"[0-9,\s\-]+", v):
                raise HTTPException(400, "tg_allowed_users 只能填 user id（数字），用逗号分隔")
            items["tg_allowed_users"] = v
        if body.tg_allowed_chats is not None:
            v = str(body.tg_allowed_chats).strip()
            if len(v) > 500:
                raise HTTPException(400, "tg_allowed_chats 过长（≤500 字符）")
            if v and not re.fullmatch(r"[0-9,\s\-]+", v):
                raise HTTPException(400, "tg_allowed_chats 只能填 chat id（数字，群组可为负），用逗号分隔")
            items["tg_allowed_chats"] = v
        if body.tg_autostart is not None:
            items["tg_autostart"] = "1" if _as_bool(body.tg_autostart, False) else "0"
        if body.x_username is not None:
            items["x_username"] = str(body.x_username).strip()[:200]
        if not items:
            raise HTTPException(400, f"没有可保存的字段；可写：{', '.join(SETTINGS_WRITABLE)}")
        await asyncio.to_thread(settings.set_many, items)
        return {"ok": True, "saved": items, "settings": settings_view(),
                "message": "设置已保存"}

    # ── SSE ──────────────────────────────────────────────
    @app.get("/api/events")
    async def api_events(request: Request, jobs: int = 50, interval: float = 3.0,
                         frames: int = 0):
        """SSE 实时状态。frames>0 时只推 N 帧就收尾（测试/一次性读取用）。"""
        interval = min(max(float(interval or 3.0), 0.2), 15.0)
        jobs_n = max(0, min(int(jobs or 0), 200))
        max_frames = max(0, int(frames or 0))

        async def gen():
            last = ""
            sent = 0
            # 首帧立即推一次，前端不用等
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.to_thread(status_payload, jobs_n)
                    data = json.dumps(payload, ensure_ascii=False)
                except Exception as e:
                    data = json.dumps({"ok": False, "error": f"状态读取失败：{e}"},
                                      ensure_ascii=False)
                frame = None
                if data != last:
                    last = data
                    frame = f"event: status\ndata: {data}\n\n"
                else:
                    frame = ": ping\n\n"
                yield frame
                if frame.startswith("event: status"):
                    sent += 1
                    if max_frames and sent >= max_frames:
                        break
                await asyncio.sleep(interval)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no",
                                          "Connection": "keep-alive"})

    return app


def _jsonable(obj: Any) -> Any:
    """把 dataclass / 异常 / 任意对象转成可 JSON 化的结构。"""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(x) for x in obj]
    if hasattr(obj, "__dataclass_fields__"):
        return {f: _jsonable(getattr(obj, f, None)) for f in obj.__dataclass_fields__}
    return str(obj)


app = None  # 供 `from web.server import app` 的调用方按需懒建；create_app() 才是标准入口


def _main() -> None:
    import uvicorn

    config.setup_console()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    host, port = config.WEB_HOST, config.WEB_PORT
    tok = expected_token()
    log.info("控制台启动：http://%s:%d/  （数据目录 %s）", host, port, config.DATA_DIR)
    if tok:
        log.info("已启用 token 校验：访问需 ?token=... 或 Authorization: Bearer ...")
    else:
        log.warning("未设置 WEB_TOKEN：任何能访问本机端口的人都能投料/发推，默认仅绑本地")
    if host not in ("127.0.0.1", "localhost", "::1") and not tok:
        log.warning("监听地址 %s 非本地且未设 WEB_TOKEN —— 强烈建议设置 WEB_TOKEN 或改回 127.0.0.1", host)
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    _main()
