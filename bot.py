#!/usr/bin/env python3
"""
Telegram -> X (Twitter) 自动发帖网关

数据流：Telegram 长轮询收料 -> SQLite 队列 -> 发布后端（X API / 无头浏览器）-> 回执回 Telegram

运行：
    cp .env.example .env  # 填 key
    python bot.py --dry-run   # 先干跑，只打印不真发（只读队列，不改状态）
    python bot.py             # 正式跑
    python bot.py --status    # 打印队列与配额后退出

发布动作由 core.pipeline + core.backends 承担（后端可在 Web 控制台切换）；
本文件只负责 Telegram 收料、命令处理与进程接线。
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import logging
import re
import signal
import sys
import time
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

# ─────────────────────────── 配置（core 为唯一来源）───────────────────────────

from core import config, media as media_mod, queue, reply as reply_mod, settings, textutil
from core.backends.base import Job
from core.lock import single_instance_lock
from core.notifier import (LogNotifier, TelegramNotifier, forget_card,
                            note_card)
from core.pipeline import Pipeline

BASE_DIR = config.BASE_DIR
DATA_DIR = config.DATA_DIR
DB_PATH = config.DB_PATH
MEDIA_DIR = config.MEDIA_DIR

TG_TOKEN = config.TG_TOKEN
ALLOWED_USERS = config.ALLOWED_USERS
ALLOWED_CHATS = config.ALLOWED_CHATS

MODE = config.MODE
TWEET_PREFIX = config.TWEET_PREFIX
TWEET_SUFFIX = config.TWEET_SUFFIX
QUOTE_MODE = config.QUOTE_MODE
DEDUP_WINDOW = config.DEDUP_WINDOW
MONTHLY_LIMIT = config.MONTHLY_LIMIT
MAX_ATTEMPTS = config.MAX_ATTEMPTS
POLL_SECONDS = config.POLL_SECONDS

DRY_RUN = False                       # 由命令行覆盖

# 兼容既有测试/调用方：从 core 转出这些函数与常量
load_dotenv = config.load_dotenv
setup_console = config.setup_console
require_tg = config.require_tg

init_db = queue.init_db
db = queue.db
now_iso = queue.now_iso
enqueue = queue.enqueue
is_dup_content = queue.is_dup_content
claim_next = queue.claim_next
mark = queue.mark
month_sent_count = queue.month_sent_count
queue_stats = queue.stats
queue_depth = queue.queue_depth

weighted_len = textutil.weighted_len
first_tweet_id = textutil.first_tweet_id
compose = textutil.compose
content_hash = textutil.content_hash

logging.basicConfig(
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("twitbot")

# ─────────────────── 相册聚合（多图支持的核心）───────────────────
#
# Telegram 的行为：用户一次选 4 张图，会陆续发来 **4 条独立消息**，它们只共享
# 一个 `media_group_id`。若不处理，就会变成 4 条独立推文。
#
# 做法：第一条到达时开一个短暂的收集窗口（ALBUM_WINDOW_SECONDS），把同组消息
# 攒在一起；窗口关闭后合并成 1 条任务入队（N 张图 + 最后一条的 caption）。
#
# 为什么用"窗口"而不是"等 media_group_id 全到齐"：Telegram 不告诉你一组有几张，
# 只能靠时间静默判断。1.5 秒对本场景足够（实测同一相册的间隔 < 300ms）。

ALBUM_WINDOW_SECONDS = 1.5

# media_group_id -> {"messages": [...], "task": asyncio.Task}
_albums: dict[str, dict] = {}


# pipeline 实例（post_init 启动，post_shutdown 取消）
# 旧版单实例引用保留给非多账号路径；账号版实例另按用户名分桶。
_pipeline: Pipeline | None = None
_pipelines: dict[str, Pipeline] = {}
_tasks: list[asyncio.Task] = []


def _pipeline_key(username: str | None = None) -> str:
    try:
        from core import accounts
        return accounts.normalize_username(username) or accounts.current_account() or "_legacy"
    except Exception:
        return (username or "_legacy").strip().lower() or "_legacy"


def get_pipeline() -> Pipeline | None:
    """当前账号的 Pipeline 实例（供 Telegram/控制台取运行态快照）。"""
    key = _pipeline_key()
    pipe = _pipelines.get(key)
    if pipe is not None:
        return pipe
    if _pipeline is not None:
        return _pipeline
    # 回退：统一启动器把常驻 pipeline 注册在 web.server 里。
    try:
        from web import server as _ws          # 延迟导入：web 不存在时也不该崩
        pipe, _why = _ws.resolve_pipeline()
        if pipe is not None:
            return pipe
    except Exception:
        pass
    return None


def set_pipeline_instance(obj: "Pipeline | None", username: str | None = None) -> None:
    """供统一启动器（start.py）注册正在跑的 Pipeline，按账号隔离。"""
    global _pipeline
    key = _pipeline_key(username)
    if obj is None:
        _pipelines.pop(key, None)
    else:
        _pipelines[key] = obj
    if key == "_legacy":
        _pipeline = obj


def ensure_pipeline(app: "Application | None" = None) -> Pipeline:
    """取当前账号常驻 pipeline；没有就现场造一个并记住。"""
    global _pipeline
    key = _pipeline_key()
    pipe = _pipelines.get(key)
    if pipe is not None:
        return pipe
    if _pipeline is not None and key == "_legacy":
        return _pipeline
    pipe = Pipeline(notifier=_make_notifier(app))
    _pipelines[key] = pipe
    if key == "_legacy":
        _pipeline = pipe
    return pipe


# ─────────────────── 运行状态计数（供 tgmanager.status()）───────────────────

_UPDATES_SEEN = 0
_LAST_UPDATE_AT = 0.0

_UPDATE_COUNT_TYPES = None      # 延迟准备，避免导入期依赖 telegram 具体版本


def updates_seen() -> int:
    """本进程累计收到的 update 数（供控制台 status()["updates"]）。"""
    return _UPDATES_SEEN


def last_update_at() -> float:
    """最近一次收到 update 的 unix 时间戳；0.0 = 从未。"""
    return _LAST_UPDATE_AT


def reset_update_stats() -> None:
    """把计数清零（机器人每次启动时调用，让控制台数字对应本次运行）。"""
    global _UPDATES_SEEN, _LAST_UPDATE_AT
    _UPDATES_SEEN = 0
    _LAST_UPDATE_AT = 0.0


async def on_any_update(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """统计用 handler（group=-1，只记账、不消费更新）。

    必须静默且**不抛异常** —— 它在每个 update 上都会跑，抛了会污染轮询。
    """
    global _UPDATES_SEEN, _LAST_UPDATE_AT
    try:
        _UPDATES_SEEN += 1
        _LAST_UPDATE_AT = time.time()
    except Exception:
        pass
    # 顺带记一份「见过的人」名册：Telegram 的 getChat 查不了私聊用户，
    # 但控制台要能用 @用户名 填管理员，所以得自己留底（见 core/tg_contacts.py）。
    try:
        u = getattr(update, "effective_user", None)
        c = getattr(update, "effective_chat", None)
        if u is not None and getattr(u, "id", None):
            from core import tg_contacts      # 延迟导入，模块缺失也不影响收料
            tg_contacts.remember(
                u.id, getattr(u, "username", "") or "",
                getattr(u, "full_name", "") or "",
                getattr(c, "id", 0) or 0, getattr(c, "type", "") or "")
    except Exception:
        pass


def _update_count_types() -> list:
    """要统计的 update 类型：只需要 `telegram.Update` 一个。

    PTB 传给 handler 的第一个参数**永远是 `telegram.Update` 包装对象**
    （`Update.ALL_TYPES` 里的 "message"/"callback_query" 是它的*字段*，
    对应的 `Message`/`CallbackQuery` 等类本身并不是 Update 的子类，
    拿它们注册 `TypeHandler` 永远不会命中）。所以这里只注册 `Update`：
    `TypeHandler` 默认 `strict=False`（走 isinstance），
    未来 PTB 出 `Update` 子类也照样命中。
    """
    global _UPDATE_COUNT_TYPES
    if _UPDATE_COUNT_TYPES is None:
        types: list = []
        try:
            types.append(Update)
        except Exception:
            pass
        _UPDATE_COUNT_TYPES = types
    return _UPDATE_COUNT_TYPES


# ─────────────────── 白名单解析（settings 优先，回落 config）───────────────────

def _parse_ids(raw: str) -> set[int]:
    """把「逗号/空白分隔的 id 串」解析成集合；非法项忽略，不抛异常。"""
    try:
        return {int(x) for x in re.split(r"[,\s]+", str(raw or "")) if x.strip().lstrip("-").isdigit()}
    except Exception:
        return set()


def _config_ids(attr: str, fallback: set[int]) -> set[int]:
    """取 config 里**当前**的白名单；拿不到则退回导入期的快照常量。

    走运行期读取（而不是直接用模块级常量）—— 这样测试/集成方 monkeypatch
    `core.config.ALLOWED_*` 能生效，也让"回落 config"是真正意义上的回落。
    """
    try:
        vals = getattr(config, attr, None)
        if vals:
            return set(vals)
    except Exception:
        pass
    return set(fallback or ())


def allowed_users_effective() -> set[int]:
    """运行期生效的 user 白名单 —— 2026-09-26 起就是**管理员列表**（core.tg_admins）。

    列表里有人就用它：控制台/机器人上批一个人**立刻生效**，不用重启进程。
    列表为空才回落老逻辑（settings.tg_allowed_users → config.ALLOWED_USERS），
    这样升级前的老部署不会突然变成「谁都不能用」。
    """
    try:
        from core import tg_admins
        tg_admins.ensure_seed()          # 第一次把老的逗号串迁进列表
        ids = tg_admins.admin_ids()
        if ids:
            return ids
    except Exception as e:
        log.debug("读管理员列表失败，回落旧白名单：%s", e)
    try:
        raw = settings.get("tg_allowed_users", "")
    except Exception:
        raw = ""
    if (raw or "").strip():
        return _parse_ids(raw)
    return _config_ids("ALLOWED_USERS", ALLOWED_USERS)


def allowed_chats_effective() -> set[int]:
    """运行期生效的 chat 白名单：settings 非空优先，为空回落 config.ALLOWED_CHATS。"""
    try:
        raw = settings.get("tg_allowed_chats", "")
    except Exception:
        raw = ""
    if (raw or "").strip():
        return _parse_ids(raw)
    return _config_ids("ALLOWED_CHATS", ALLOWED_CHATS)


# ─────────────────────── Telegram 收料 ───────────────────────

async def _authorized(update: Update) -> tuple[bool, str]:
    chat = update.effective_chat
    msg = update.effective_message
    if chat is None or msg is None:
        return False, ""
    allowed_chats = allowed_chats_effective()
    allowed_users = allowed_users_effective()
    if allowed_chats and chat.id not in allowed_chats:
        return False, "此会话不在 ALLOWED_CHATS 白名单内"
    if allowed_users:
        uid = msg.from_user.id if msg.from_user else None
        if uid not in allowed_users:
            return False, f"发送者 {uid} 不在管理员列表里"
    return True, ""


async def _hint_not_admin(update: Update, why: str) -> None:
    """非管理员发消息时回一句提示（用户要求：不能默默不理）。

    群里不回 —— 免得在别人的群里刷屏；私聊才提示。
    """
    msg = update.effective_message
    if msg is None:
        return
    chat = getattr(msg, "chat", None)
    if chat is not None and getattr(chat, "type", "") not in ("private", ""):
        return
    try:
        await msg.reply_text(
            "🔒 你还不是管理员，不能给机器人发料。\n\n"
            "想申请使用？发送：/sign\n"
            "申请会转给管理员审批（一个月只能申请一次）。")
    except Exception as e:
        log.debug("提示非管理员失败：%s", e)


async def _notify_owner_new_signup(bot_obj, row: dict) -> bool:
    """把新申请推给所有者，带「批准 / 拒绝」按钮。"""
    try:
        from core import tg_admins
        oid = tg_admins.owner_id()
    except Exception:
        return False
    if not oid or bot_obj is None:
        log.warning("没有所有者 —— 这条申请没人能批")
        return False
    who = row.get("name") or "（没名字）"
    uname = f"（@{row['username']}）" if row.get("username") else ""
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 批准", callback_data=f"sg:ok:{row['id']}"),
        InlineKeyboardButton("❌ 拒绝", callback_data=f"sg:no:{row['id']}"),
    ]])
    text = (f"🔔 新的管理员申请\n\n"
            f"👤 {who}{uname}\n"
            f"🆔 {row['user_id']}\n"
            f"🕐 {(row.get('created_at') or '')[:19].replace('T', ' ')} UTC\n\n"
            f"批准后 TA 就能给机器人发料了。")
    try:
        await bot_obj.send_message(oid, text, reply_markup=kb)
        return True
    except Exception as e:
        # 只记异常类型：异常文本里可能带 token
        log.warning("通知所有者失败（%s）—— 对方要先给机器人发过消息",
                    type(e).__name__)
        return False


def _caller_is_admin(update: Update) -> bool:
    """当前发消息的人是不是管理员（白名单为空 = 不限制，算 True）。"""
    try:
        ids = allowed_users_effective()
        u = update.effective_user
        if not ids:
            return True
        return bool(u is not None and u.id in ids)
    except Exception:
        return False


async def _maybe_sign_command(update: Update, msg) -> bool:
    """识别 `/sign` 申请。返回 True = 已处理（调用方直接 return）。

    必须在 `_authorized` **之前**调用 —— 申请人本来就还不是管理员。
    """
    txt = (getattr(msg, "text", "") or "").strip()
    if not txt:
        return False
    low = txt.lower()
    if low not in ("/sign", "/申请"):
        # 裸写 "sign" 只对**非管理员**当命令 —— 管理员可能真想发这个内容
        if low != "sign" or _caller_is_admin(update):
            return False
    # 配了会话白名单就让白名单说话（别让别的群乱申请）
    try:
        allowed_chats = allowed_chats_effective()
        chat = update.effective_chat
        if allowed_chats and (chat is None or chat.id not in allowed_chats):
            return False
    except Exception:
        pass
    await _handle_sign(update, msg)
    return True


async def _handle_sign(update: Update, msg) -> None:
    """`/sign` —— 申请使用权限。"""
    u = getattr(msg, "from_user", None)
    uid = getattr(u, "id", 0) or 0
    try:
        from core import tg_admins
    except Exception as e:
        await msg.reply_text(f"申请功能不可用：{type(e).__name__}")
        return
    ok, why, row = tg_admins.create_signup(
        uid, getattr(u, "username", "") or "", getattr(u, "full_name", "") or "")
    if not ok:
        await msg.reply_text(f"⚠️ {why}")
        return
    await msg.reply_text(
        "✅ 申请已提交，已转给管理员审批。\n"
        "批下来我会立刻通知你，之后就能正常给机器人发料了。")
    sent = await _notify_owner_new_signup(msg.get_bot(), row or {})
    if not sent:
        log.warning("申请 #%s 没能通知到所有者", (row or {}).get("id"))


async def _save_media(msg, kind: str, *, slot: int = 0) -> str:
    """下载到 MEDIA_DIR，返回**相对** MEDIA_DIR 的文件名（换机器/挪目录不失效）。

    `slot`：同一相册里该图是第几张。多图时文件名带序号，避免覆盖
    （相册的消息 id 各不相同，但加序号更直观、也便于排障）。
    """
    if kind == "photo":
        # Telegram 的 photo 是一组不同分辨率，取最大的那个
        f = await msg.photo[-1].get_file()
        suffix = ".jpg"
    elif kind == "video":
        f = await msg.video.get_file()
        suffix = ".mp4"
    else:
        doc = msg.document
        suffix = Path(doc.file_name or "bin").suffix or ".bin"
        f = await doc.get_file()
    tail = f"_{slot}" if slot else ""
    name = f"{msg.chat_id}_{msg.message_id}{tail}{suffix}"
    await f.download_to_drive(custom_path=str(MEDIA_DIR / name))
    return name


def resolve_media(stored: str) -> Path | None:
    """把库里存的相对名解析成绝对路径（兼容历史绝对路径记录）。

    契约层等价实现见 `core.backends.base.Job.resolved_media`。
    """
    return Job(id=0, kind="", media_path=stored).resolved_media(MEDIA_DIR)


# ─────────────────── 发送 / 设置 面板 ───────────────────

async def _show_send(target) -> None:
    """「📤 发送」面板：先选类型。"""
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 纯文字", callback_data="sendtext"),
         InlineKeyboardButton("🖼 图文", callback_data="sendmedia")],
        [InlineKeyboardButton("🔁 转帖 / 引用 / 评论", callback_data="sendreply")],
        [InlineKeyboardButton("⬅️ 返回", callback_data="menu")],
    ])
    await target.reply_text(
        "📤 发送内容\n\n请选择类型：\n"
        "· 纯文字 —— 只发文字\n"
        "· 图文 —— 图片 + 配文（最多 4 张）\n"
        "· 转帖 / 引用 / 评论 —— 对某条推文动手\n\n"
        "更快的办法：直接把推文链接粘给我，我会问你要做什么。",
        reply_markup=kb)


async def _show_action_panel(target, link: str = "") -> None:
    """「这条推文要做什么？」：转帖 / 引用 / 评论 三选一。

    三种操作在 X 侧是**三条完全不同的路径**（CreateRetweet vs
    带 quote_tweet_id 的 CreateTweet vs 带 in_reply_to_tweet_id 的
    CreateTweet），所以必须让用户先明确选，机器人不猜。
    """
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 转帖", callback_data="op:retweet"),
         InlineKeyboardButton("✏️ 引用", callback_data="op:quote")],
        [InlineKeyboardButton("💬 评论", callback_data="op:reply")],
        [InlineKeyboardButton("❌ 取消", callback_data="op:cancel")],
    ])
    head = "🔁 推文操作\n"
    if link:
        head += f"目标：{link}\n"
    await target.reply_text(
        head + "\n要对这条推文做什么？\n"
        "· 🔄 转帖 —— 直接转到我的主页（不带文字/图片）\n"
        "· ✏️ 引用 —— 带上我写的文字一起发\n"
        "· 💬 评论 —— 回复到这条推文下面",
        reply_markup=kb)


async def _show_reply_mode_panel(target) -> None:
    """评论的形态：纯文字 / 图文。"""
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 只发文字", callback_data="opreply:text"),
         InlineKeyboardButton("🖼 图文", callback_data="opreply:media")],
        [InlineKeyboardButton("⬅️ 返回", callback_data="op:back")],
    ])
    await target.reply_text(
        "💬 评论\n\n这条评论是纯文字，还是带图片？\n"
        "· 只发文字 —— 评论只有文字\n"
        "· 图文 —— 图片 + 评论文字（最多 4 张）",
        reply_markup=kb)


async def _prompt_send(target, mode: str) -> None:
    """提示用户把内容发过来。"""
    if mode == "text":
        tips = "请把要发的文字发给我。"
    else:
        tips = ("请把图片发给我（可多选，最多 4 张），"
                "并在**同一条消息**里写好配文。")
    await target.reply_text(f"{tips}\n\n（发完会先给你确认卡，不会直接发出去）")


async def _show_settings(target) -> None:
    """设置面板：队列发送开关 + 发送间隔。"""
    on = True
    gap = 0
    try:
        on = settings.queue_send_enabled()
        gap = settings.get_int("send_interval_minutes", 0)
    except Exception:
        pass
    switch = "✅ 已开启（自动按间隔发）" if on else "⛔ 已关闭（只入库，手动发）"
    gap_text = "不限制" if gap <= 0 else f"{gap} 分钟"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"队列发送：{'关闭' if on else '开启'}",
                              callback_data="toggle_queue_send")],
        [InlineKeyboardButton("⏱ 改发送间隔", callback_data="set_interval")],
        [InlineKeyboardButton("⬅️ 返回", callback_data="menu")],
    ])
    await target.reply_text(
        f"⚙️ 设置\n\n"
        f"队列发送：{switch}\n"
        f"发送间隔：{gap_text}\n\n"
        f"「队列发送」关掉后，内容只入库，必须你手动点「🚀 发送」。",
        reply_markup=kb)


async def _show_interval_picker(target) -> None:
    """发送间隔选择：常用值一键选 + 自定义。"""
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("5 分钟", callback_data="interval:5"),
         InlineKeyboardButton("15 分钟", callback_data="interval:15")],
        [InlineKeyboardButton("30 分钟", callback_data="interval:30"),
         InlineKeyboardButton("1 小时", callback_data="interval:60")],
        [InlineKeyboardButton("2 小时", callback_data="interval:120"),
         InlineKeyboardButton("不限制", callback_data="interval:0")],
        [InlineKeyboardButton("⬅️ 返回", callback_data="settings")],
    ])
    await target.reply_text(
        "⏱ 选择发送间隔\n\n两条推文之间**至少**隔这么久（防连发被风控）。",
        reply_markup=kb)


async def _show_admins(target) -> None:
    """管理员**列表**面板（含待审批）。"""
    try:
        from core import tg_admins
    except Exception as e:
        await target.reply_text(f"管理员模块不可用：{type(e).__name__}")
        return
    tg_admins.ensure_seed()
    rows = tg_admins.admins()
    if rows:
        cur = "\n".join(
            f"  {'👑' if r['is_owner'] else '·'} {r['name'] or '（无名）'}"
            f"{' @' + r['username'] if r['username'] else ''} · {r['user_id']}"
            for r in rows)
    else:
        cur = "  （空 —— 列表为空时不限制任何人，建议至少留一个）"
    pend = tg_admins.pending_signups()
    extra = f"\n\n⏳ 待审批：{len(pend)} 条" if pend else ""
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ 把当前会话设为管理员",
                              callback_data="admin_here")],
        [InlineKeyboardButton("⬅️ 返回", callback_data="backends")],
    ])
    await target.reply_text(
        f"👤 管理员列表（{len(rows)} 人）\n\n{cur}{extra}\n\n"
        f"👑 = 所有者（默认是列表里第一个，可在控制台改）\n"
        f"别人发 /sign 申请；你收到通知点「批准」即可。\n\n"
        f"用 @用户名 直接加人（不用抄数字）：\n"
        f"  添加管理员 @用户名\n"
        f"  删除管理员 @用户名",
        reply_markup=kb)


# ─────────────────── 交互界面（菜单 / 队列 / 预览）───────────────────

def _status_line() -> str:
    """一行状态摘要，菜单与回执都用它。"""
    try:
        st = queue.stats()
        depth = st.get("depth", st.get("total", 0))
        sent = st.get("month_sent", 0)
        limit = st.get("limit", 0)
        be = settings.current_backend()
        paused = " · ⏸已暂停" if settings.is_paused() else ""
        return f"待办 {depth} · 本月 {sent}/{limit} · 后端 {be}{paused}"
    except Exception:
        return "状态读取失败"


async def _show_menu(target) -> None:
    """主菜单。"""
    paused = False
    try:
        paused = settings.is_paused()
    except Exception:
        pass
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 发送", callback_data="send"),
         InlineKeyboardButton("📋 队列", callback_data="queue")],
        [InlineKeyboardButton("⚙️ 设置", callback_data="settings"),
         InlineKeyboardButton("🔌 后端设置", callback_data="backends")],
        [InlineKeyboardButton("📊 数据日报", callback_data="an")],
        [InlineKeyboardButton("▶️ 恢复发布" if paused else "⏸ 暂停发布",
                              callback_data="resume" if paused else "pause")],
    ])
    text = ("🤖 TwitBot\n"
            f"{_status_line()}\n\n"
            "· 📤 发送 —— 选「纯文字 / 图文」，然后发内容\n"
            "· 🔁 对别人的推文动手？直接粘推文链接给我，\n"
            "   我会问你「转帖 / 引用 / 评论」\n"
            "· 📋 队列 —— 逐条预览 / 发送 / 修改\n"
            "· ⚙️ 设置 —— 队列发送开关、发送间隔\n"
            "· 📊 数据日报 —— 浏览/点赞，拆「认证/普通」\n"
            "   （也可以直接发「日报」「三月」「单条 <推文ID>」）")
    try:
        await target.reply_text(text, reply_markup=kb)
    except Exception:
        await target.reply_text(text, reply_markup=kb)


def _target_line(quote_id) -> str:
    """把 jobs.quote_id 翻成一行给人看的说明；无目标返回空串。

    转帖 / 引用 / 评论在库里共用一列，用户看到的必须能分清是哪种，
    否则"我明明选的转帖，怎么出了条推文"这类问题无从排查。
    """
    kind = reply_mod.target_kind(quote_id)
    if not kind:
        return ""
    tid = reply_mod.target_id(quote_id)
    url = reply_mod.tweet_url(tid)
    label = {"retweet": "🔄 转帖", "quote": "✏️ 引用", "reply": "💬 评论"}[kind]
    return f"{label} {url}"


def _job_tag(row) -> str:
    """一行的图标 + 类型说明。"""
    media = media_mod.parse_media_paths(row["media_path"] if "media_path" in row.keys() else "")
    tkind = reply_mod.target_kind(row["quote_id"] if "quote_id" in row.keys() else "")
    if media:
        n = len(media)
        tag = f"🖼 {n}图" + ("+字" if (row["raw_text"] or "").strip() else "")
    else:
        tag = "📝 纯文字"
    # 转帖没有正文也没有媒体，单独一个标记，否则列表里会显示成"纯文字"
    if tkind == "retweet":
        return "🔄 转帖"
    if tkind == "reply":
        return f"💬 {tag}"
    return tag


async def _show_queue(target) -> None:
    """待办列表：每条带 [预览][发这条][删]。"""
    rows = queue.recent(limit=20)
    items = [r for r in rows if r["status"] in ("pending", "awaiting", "failed")]
    if not items:
        await target.reply_text("📋 待办为空")
        return

    # Telegram 单条消息最多 100 个按钮；每条任务占一行按钮，显示 5 条即可
    show = items[:5]
    lines = ["📋 待办队列（显示前 5 条）\n"]
    kb_rows: list[list] = []
    for r in show:
        jid = r["id"]
        text = (r["raw_text"] or "").strip().replace("\n", " ")[:24]
        flag = " ⚠️失败" if r["status"] == "failed" else (
            " ⏳待确认" if r["status"] == "awaiting" else "")
        lines.append(f"#{jid} {_job_tag(r)}{flag}  {text or '（无文字）'}")
        if r["status"] == "failed":
            kb_rows.append([
                InlineKeyboardButton("👁 预览", callback_data=f"preview:{jid}"),
                InlineKeyboardButton("🔄 重试", callback_data=f"retry:{jid}"),
                InlineKeyboardButton("❌ 删", callback_data=f"no:{jid}"),
            ])
        else:
            # 转帖没有正文可改，"修改"按钮点了也没意义，索性不显示
            is_retweet = reply_mod.target_kind(
                r["quote_id"] if "quote_id" in r.keys() else "") == "retweet"
            row_btns = [
                InlineKeyboardButton("👁 预览", callback_data=f"preview:{jid}"),
                InlineKeyboardButton("🚀 发送", callback_data=f"pub:{jid}"),
            ]
            if not is_retweet:
                row_btns.append(InlineKeyboardButton("✏️ 修改", callback_data=f"edit:{jid}"))
            row_btns.append(InlineKeyboardButton("❌ 删除", callback_data=f"no:{jid}"))
            kb_rows.append(row_btns)
    kb_rows.append([InlineKeyboardButton("🔄 全部重试", callback_data="menu"),
                    InlineKeyboardButton("⬅️ 返回", callback_data="menu")])
    await target.reply_text("\n".join(lines),
                            reply_markup=InlineKeyboardMarkup(kb_rows))


async def _send_preview(target, job_id: int) -> None:
    """展开一条任务的完整内容。"""
    r = queue.get(job_id)
    if r is None:
        await target.reply_text(f"#{job_id} 不存在")
        return
    media = media_mod.parse_media_paths(r["media_path"] or "")
    body = compose(settings.effective_prefix(), r["raw_text"] or "",
                   settings.effective_suffix())
    head = f"👁 #{job_id} · {_job_tag(r)} · {r['status']}"
    tail = f"\n\n（{weighted_len(body)}/280 字）"
    tline = _target_line(r["quote_id"])
    if tline:
        tail += "\n" + tline
    if media:
        tail += f"\n图片：{len(media)} 张"
    if r["error"]:
        tail += f"\n⚠️ 上次失败：{str(r['error'])[:150]}"
    # 转帖没有正文，"修改文字"点了也没东西可改
    btns = [InlineKeyboardButton("🚀 发送", callback_data=f"pub:{job_id}")]
    if reply_mod.target_kind(r["quote_id"]) != "retweet":
        btns.append(InlineKeyboardButton("✏️ 修改文字", callback_data=f"edit:{job_id}"))
    btns.append(InlineKeyboardButton("❌ 删除", callback_data=f"no:{job_id}"))
    kb = InlineKeyboardMarkup([btns])
    await target.reply_text(f"{head}\n\n{body or '（无文字）'}{tail}", reply_markup=kb)


async def _send_status(target) -> None:
    st = queue.stats()
    by = "  ".join(f"{k}={v}" for k, v in (st.get("by_status") or {}).items()) or "空"
    await target.reply_text(
        f"📊 状态\n\n队列：{by}\n"
        f"本月已发：{st.get('month_sent', 0)}/{st.get('limit', 0)}\n"
        f"后端：{settings.current_backend()}"
        f"{'（已暂停）' if settings.is_paused() else ''}")


# ══════════════════════════════════════════════════════════
# 数据日报（core.analytics）
# ══════════════════════════════════════════════════════════

AN_PAGE_SIZE = 8          # 推文列表一页几条（Telegram 按钮别太多）
AN_SUMMARY_DAYS = 92      # 近三月


def _an_num(n) -> str:
    try:
        return f"{int(n or 0):,}"
    except Exception:
        return "0"


def _an_module():
    """延迟导入；模块缺失返回 None（机器人其余功能照常）。"""
    try:
        from core import analytics
        return analytics
    except Exception as e:
        log.warning("数据日报模块不可用：%s", e)
        return None


def _an_metric_lines(metrics, keys=None, limit=7) -> list[str]:
    out = []
    for m in metrics or []:
        if keys and m.get("key") not in keys:
            continue
        out.append(f"{m['label']} {_an_num(m['total'])}"
                   f"（✅{_an_num(m['verified'])} / ⚪{_an_num(m['normal'])}）")
        if len(out) >= limit:
            break
    return out


def _an_summary_text(an) -> str:
    s = an.summary(AN_SUMMARY_DAYS)
    sp = s.get("today_split") or {}
    bot = sp.get("bot") or {"count": 0, "views": 0}
    man = sp.get("manual") or {"count": 0, "views": 0}
    lines = [f"📊 数据日报 · {s.get('today', '')}", ""]
    lines.append("【今日实时】今天发的帖子（当前值）")
    lines.append(f"发帖 {_an_num(sp.get('count'))} 条 · 浏览 {_an_num(sp.get('views'))}"
                 f" · 点赞 {_an_num(sp.get('likes'))}")
    lines.append(f"🤖 机器人 {_an_num(bot.get('count'))} 条 / "
                 f"👤 自己 {_an_num(man.get('count'))} 条")
    lines.append("")
    acc_day = s.get("account_day") or ""
    if acc_day:
        tag = "" if s.get("account_is_today") else "（X 最新完整日）"
        lines.append(f"【账号数据 · {acc_day}】{tag}")
        lines.append("（✅=认证用户 ⚪=普通用户）")
        lines += _an_metric_lines(s.get("account_metrics"), limit=6)
    else:
        lines.append("【账号数据】还没有采集到")
    lines.append("")
    lines.append(f"【近三月 {s.get('range_from', '')} ~ {s.get('range_to', '')}】")
    lines.append("（✅=认证用户 ⚪=普通用户）")
    lines += _an_metric_lines(s.get("range_metrics"), limit=4)
    if not s.get("account_day") and not s.get("today_posts"):
        lines.append("")
        lines.append("（还没采集过数据，点下面的「🔄 采集」）")
    return "\n".join(lines)


def _an_tweet_line(t) -> str:
    icon = "🤖" if t.get("source") == "bot" else "👤"
    kind = {"orig": "原创", "quote": "引用", "reply": "回复",
            "retweet": "转帖"}.get(t.get("kind"), t.get("kind") or "")
    when = (t.get("created_at") or "")[5:16].replace("T", " ")
    head = (t.get("text") or "").replace("\n", " ")[:34]
    return (f"{when} {icon} {kind}\n"
            f"👁{_an_num(t.get('views'))} ❤{_an_num(t.get('likes'))} "
            f"💬{_an_num(t.get('replies'))} ↻{_an_num(t.get('retweets'))}\n"
            f"{head}")


def _an_tweet_kb(rows, day: str, page: int) -> InlineKeyboardMarkup:
    step = AN_PAGE_SIZE
    chunk = rows[page * step:(page + 1) * step]
    kb_rows = [[InlineKeyboardButton(
        f"#{t['tweet_id'][-6:]}  {_an_num(t.get('views'))}浏览 ❤{_an_num(t.get('likes'))}",
        callback_data=f"ant:{t['tweet_id']}")] for t in chunk]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ 上一页", callback_data=f"anp:{day}:{page - 1}"))
    if (page + 1) * step < len(rows):
        nav.append(InlineKeyboardButton("下一页 ▶", callback_data=f"anp:{day}:{page + 1}"))
    if nav:
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton("⬅️ 返回日报", callback_data="an"),
                    InlineKeyboardButton("🔄 采集", callback_data="anr")])
    return InlineKeyboardMarkup(kb_rows)


async def _an_show_tweets(target, day: str, page: int = 0) -> None:
    an = _an_module()
    if an is None:
        await target.reply_text("数据日报模块不可用，请看控制台日志")
        return
    rows = an.tweets(day=day, limit=400) if day else an.tweets(days=1, limit=400)
    if not rows:
        await target.reply_text(f"📭 {day or '今天'} 还没有推文记录")
        return
    total_pages = max(1, (len(rows) + AN_PAGE_SIZE - 1) // AN_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    body = "\n\n".join(_an_tweet_line(t)
                       for t in rows[page * AN_PAGE_SIZE:(page + 1) * AN_PAGE_SIZE])
    await target.reply_text(
        f"📋 {day or '今天'} 的推文（{len(rows)} 条，第 {page + 1}/{total_pages} 页）\n"
        f"点下面的按钮看单条详情\n\n{body}",
        reply_markup=_an_tweet_kb(rows, day, page))


def _an_tweet_text(t) -> str:
    icon = "🤖 机器人发" if t.get("source") == "bot" else "👤 自己发"
    kind = {"orig": "原创", "quote": "引用", "reply": "回复",
            "retweet": "转帖"}.get(t.get("kind"), t.get("kind") or "")
    lines = [f"📌 {t.get('tweet_id')}",
             f"🕐 {t.get('created_at') or '—'}（UTC）",
             f"{icon} · {kind}" + (f" · 🖼 {t['media_count']} 张图"
                                   if t.get("media_count") else ""),
             "",
             f"👁 浏览量 {_an_num(t.get('views'))}",
             f"❤ 点赞 {_an_num(t.get('likes'))}   💬 评论 {_an_num(t.get('replies'))}",
             f"↻ 转发 {_an_num(t.get('retweets'))}   ❝ 引用 {_an_num(t.get('quotes'))}"
             f"   🔖 收藏 {_an_num(t.get('bookmarks'))}"]
    a = t.get("activity")
    if a:
        lines.append("")
        lines.append(f"📊 曝光 {_an_num(a.get('impressions'))} · 参与 {_an_num(a.get('engagements'))}")
        lines.append(f"详情展开 {_an_num(a.get('detail_expands'))} · "
                     f"资料访问 {_an_num(a.get('profile_visits'))} · "
                     f"涨粉 {_an_num(a.get('follows'))}")
    if t.get("text"):
        lines += ["", t["text"][:280]]
    return "\n".join(lines)


async def _an_show_tweet(target, tweet_id: str) -> None:
    an = _an_module()
    if an is None:
        await target.reply_text("数据日报模块不可用")
        return
    tid = textutil.first_tweet_id(tweet_id) or str(tweet_id or "").strip()
    t = an.tweet(tid) if tid else None
    if not t:
        await target.reply_text(f"没查到推文 {tid or tweet_id}（可能还没采集到）")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🐦 在 X 打开", url=t["url"]),
        InlineKeyboardButton("⬅️ 返回日报", callback_data="an"),
    ]])
    await target.reply_text(_an_tweet_text(t), reply_markup=kb,
                            disable_web_page_preview=True)


async def _an_refresh(target) -> None:
    an = _an_module()
    if an is None:
        await target.reply_text("数据日报模块不可用")
        return
    await target.reply_text("🔄 开始采集（要开浏览器，约 20~60 秒），完成后我把日报发给你")

    async def _work():
        res = await asyncio.to_thread(an.refresh, AN_SUMMARY_DAYS)
        if not res.get("ok"):
            await target.reply_text(f"❌ 采集失败：{res.get('error') or '未知原因'}")
            return
        await target.reply_text("✅ 采集完成")
        await _an_send_summary(target)

    asyncio.create_task(_work())


async def _an_send_summary(target) -> None:
    an = _an_module()
    if an is None:
        await target.reply_text("数据日报模块不可用")
        return
    s = an.summary(AN_SUMMARY_DAYS)
    day = s.get("account_day") or s.get("today") or ""
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 今日推文列表", callback_data=f"anl:{s.get('today', '')}"),
         InlineKeyboardButton("📋 昨日推文列表", callback_data=f"anl:{day}")],
        [InlineKeyboardButton("🔄 采集", callback_data="anr")],
    ])
    await target.reply_text(_an_summary_text(an), reply_markup=kb)


async def _an_show_day(target, day: str) -> None:
    """某一天的账号级数据（认证/普通拆分）+ 当天发帖汇总。"""
    an = _an_module()
    if an is None:
        await target.reply_text("数据日报模块不可用")
        return
    d = an.daily(day)
    lines = [f"📊 {day} 账号数据"]
    ml = _an_metric_lines(d.get("metrics"), limit=10)
    lines += ml or ["（这一天没有账号级数据 —— 超出采集范围）"]
    lines += ["", f"当天发帖 {_an_num(d.get('posts'))} 条 · "
                  f"浏览 {_an_num(d.get('post_views'))}"]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 当天推文列表", callback_data=f"anl:{day}"),
        InlineKeyboardButton("⬅️ 返回日报", callback_data="an"),
    ]])
    await target.reply_text("\n".join(lines), reply_markup=kb)


async def _maybe_an_command(msg, text: str) -> bool:
    """数据日报的文本命令。返回 True = 已处理，调用方应 return。"""
    t = (text or "").strip()
    if not t:
        return False
    low = t.lower().lstrip("/")

    m = re.match(r"^(?:单条|推文|tweet)\s+(\S+)$", t, re.IGNORECASE)
    if m:
        await _an_show_tweet(msg, m.group(1))
        return True
    m = re.match(r"^(?:日报|daily)\s+(\d{4}-\d{2}-\d{2})$", t, re.IGNORECASE)
    if m:
        await _an_show_day(msg, m.group(1))
        return True
    if low in ("日报", "daily", "今日日报"):
        await _an_send_summary(msg)
        return True
    if low in ("三月", "近三月", "近3月", "3月", "month"):
        an = _an_module()
        if an is None:
            await msg.reply_text("数据日报模块不可用")
            return True
        s = an.overview(AN_SUMMARY_DAYS)
        lines = [f"📈 近三月 {s['from']} ~ {s['to']}"]
        lines.append("（✅=认证用户 ⚪=普通用户）")
        lines += _an_metric_lines(s.get("metrics"), limit=10)
        lines += ["", f"发帖 {_an_num(s.get('posts'))} 条 · "
                      f"浏览 {_an_num(s.get('post_views'))}"]
        await msg.reply_text("\n".join(lines))
        return True
    if low in ("采集日报", "刷新日报", "采集数据", "刷新数据"):
        await _an_refresh(msg)
        return True
    return False


async def _add_admin_by_query(msg, raw: str, *, remove: bool = False) -> None:
    """用 `@用户名` 或数字 id 增删管理员。

    白名单里只存数字 user id，但让人去抄一长串数字太反人类，
    所以这里用 Telegram 的 getChat 把 @用户名 换算成 id。
    （只有机器人见过的人才能被解析 —— 让对方先给机器人发一句 /start。）
    """
    q = (raw or "").strip().lstrip("@")
    if not q:
        await msg.reply_text("用法：\n添加管理员 @用户名\n删除管理员 12345678")
        return
    label = ""
    if re.fullmatch(r"-?\d+", q):
        uid = q
    else:
        # 先查本地名册（Telegram 的 getChat 查不了私聊用户）
        try:
            from core import tg_contacts
            hit = tg_contacts.lookup(q)
        except Exception:
            hit = None
        if hit:
            uid = str(hit["id"])
            label = tg_contacts.display(hit)
        else:
            try:
                chat = await msg.get_bot().get_chat("@" + q)
                uid = str(chat.id)
                label = getattr(chat, "full_name", "") or getattr(chat, "username", "") or ""
            except Exception as e:
                await msg.reply_text(
                    f"没能把 @{q} 换算成 user id（{type(e).__name__}）。\n"
                    f"让 TA 先给机器人发一句 /start（机器人会自动记下），再试一次。")
                return

    cur = settings.get("tg_allowed_users", "") or ""
    ids = [x.strip() for x in cur.split(",") if x.strip()]
    if remove:
        if uid not in ids:
            await msg.reply_text(f"user id {uid} 不在管理员列表里")
            return
        ids = [x for x in ids if x != uid]
    else:
        if uid in ids:
            await msg.reply_text(f"user id {uid} 已经是管理员了")
            return
        ids.append(uid)
    settings.set_many({"tg_allowed_users": ",".join(dict.fromkeys(ids))})
    who = f"{label}（{uid}）" if label else uid
    await msg.reply_text(
        f"{'❌ 已移除管理员' if remove else '✅ 已加为管理员'}：{who}\n"
        f"当前管理员：{', '.join(ids) or '（空 = 不限制）'}")


async def _maybe_admin_command(msg, text: str) -> bool:
    """管理员增删的文本命令。返回 True = 已处理。"""
    t = (text or "").strip()
    if not t:
        return False
    m = re.match(r"^/?(?:添加管理员|加管理员|addadmin|add_admin)\s*(.*)$",
                 t, re.IGNORECASE)
    if m:
        await _add_admin_by_query(msg, m.group(1))
        return True
    m = re.match(r"^/?(?:删除管理员|移除管理员|deladmin|del_admin)\s*(.*)$",
                 t, re.IGNORECASE)
    if m:
        await _add_admin_by_query(msg, m.group(1), remove=True)
        return True
    return False


async def _show_backends(target) -> None:
    """后端选择：只看可用性，不可用的点了会被拒。"""
    try:
        from core import backends as _be
        items = _be.describe()
    except Exception as e:
        await target.reply_text(f"读取后端失败：{type(e).__name__}: {e}")
        return
    cur = settings.current_backend()
    lines = ["⚙️ 发布后端\n"]
    kb_rows = []
    for it in items:
        mark_ = "✅" if it["available"] else "⛔"
        now = " ←当前" if it["name"] == cur else ""
        lines.append(f"{mark_} {it['label']}（{it['name']}）{now}")
        if not it["available"]:
            lines.append(f"      {str(it['reason'])[:60]}")
        if it["name"] != cur:
            kb_rows.append([InlineKeyboardButton(
                f"切到 {it['label']}", callback_data=f"setbe:{it['name']}")])
    kb_rows.append([InlineKeyboardButton("👤 管理员 ID", callback_data="admins")])
    kb_rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="menu")])
    await target.reply_text("\n".join(lines),
                            reply_markup=InlineKeyboardMarkup(kb_rows))


async def _switch_backend(target, name: str) -> None:
    """切换后端 —— **不可用就拒绝**，否则任务会静默堆积。"""
    try:
        from core import backends as _be
        be = _be.get(name)
        ok, why = be.available()
    except Exception as e:
        await target.reply_text(f"后端 {name} 不可用：{type(e).__name__}: {e}")
        return
    if not ok:
        await target.reply_text(f"⛔ 不能切到 {name}：{why}")
        return
    settings.set_many({"backend": name})
    await target.reply_text(f"✅ 已切到 {name}")
    await _show_backends(target)


# ─────────────────── "发送" 流程状态 ───────────────────
#
# 用户点「📤 发送」-> 选「纯文字 / 图文」-> 发内容 -> 出确认卡
#   [📥 添加到队列] [🚀 实时发送] [✏️ 修改] [❌ 取消]
#
# 用内存字典按 chat 记（重启即清空，这是有意的：半途的状态不该跨重启）。

_send_mode: dict[int, str] = {}      # chat_id -> "text" | "media"


def _mark_send_mode(chat_id: int, mode: str) -> None:
    _send_mode[int(chat_id)] = str(mode)


def _take_send_mode(chat_id: int) -> str:
    """取出并清除（只对下一条消息生效）。"""
    return _send_mode.pop(int(chat_id), "")


def _peek_send_mode(chat_id: int) -> str:
    return _send_mode.get(int(chat_id), "")


# ─────────────────── "推文操作" 流程状态 ───────────────────
#
# 顺序刻意是「**先拿链接，再问做什么**」：
#
#   用户直接发链接  ->  stage="choose"  机器人问「转帖 / 引用 / 评论」
#   点「发送 → 转帖/引用/评论」 -> stage="link"  机器人先索要链接
#
# 为什么不是之前那样「先问评论是文字还是图文」：用户还没说要对哪条推文
# 动手，问形态没有意义；而且那时候机器人也还不知道用户要的是转帖/引用，
# 三个操作的内容要求完全不同（转帖不要文字、引用要整段文字、评论才分
# 纯文字/图文）。先锁目标，再让用户点一个操作，出错点才单一。
#
# 状态字段：
#   stage  "link"    等用户发目标推文链接
#          "choose"  已有目标，等用户点「转帖 / 引用 / 评论」
#          "content" 已有目标和操作，等用户发内容
#   op     "" | "retweet" | "quote" | "reply"
#   mode   "" | "text" | "media"      （只有评论用得到）
#   target 目标推文 id（纯数字）
#   src    用户发来链接的那条消息 id（转帖任务拿它当去重键；重投同一条
#          update 时不会重复转推同一推文）

_reply_state: dict[int, dict] = {}   # chat_id -> {"stage","op","mode","target","src"}


def _mark_reply(chat_id: int, mode: str = "", *, target: str = "",
                stage: str = "link", src: int = 0) -> None:
    """进入推文操作流程。

    `target` 非空表示链接已经拿到（用户直接粘链接进来的场景），
    此时 stage 应为 "choose"，等着用户点操作。
    """
    _reply_state[int(chat_id)] = {
        "stage": str(stage), "op": "", "mode": str(mode),
        "target": str(target or ""), "src": int(src or 0),
    }


def _peek_reply(chat_id: int) -> dict:
    return _reply_state.get(int(chat_id)) or {}


def _set_reply_target(chat_id: int, target: str, src: int = 0) -> None:
    """记下目标推文，进入「选操作」阶段。"""
    st = _reply_state.get(int(chat_id))
    if st is None:
        return
    st["stage"] = "choose"
    st["target"] = str(target)
    if src:
        st["src"] = int(src)


def _set_reply_op(chat_id: int, op: str, mode: str = "") -> None:
    """记下用户选的操作（转帖/引用/评论），进入「等内容」阶段。"""
    st = _reply_state.get(int(chat_id))
    if st is None:
        return
    st["stage"] = "content"
    st["op"] = str(op)
    if mode:
        st["mode"] = str(mode)


def _take_reply(chat_id: int) -> dict:
    """取出并**清除**推文操作流程状态（内容收到后调用）。"""
    return _reply_state.pop(int(chat_id), None) or {}


def _reply_quote_id(rstate: dict) -> str:
    """把流程状态翻成 jobs.quote_id 里的目标编码。

    转帖 -> `t:<id>`；评论 -> `r:<id>`；引用 -> 纯数字。
    目标缺失或操作未知时返回空串（调用方必须当成"流程不完整"处理，
    绝不能退回普通发帖 —— 那会发出一条脱离目标的孤儿帖）。
    """
    target = str(rstate.get("target") or "").strip()
    if not target:
        return ""
    op = str(rstate.get("op") or "").strip().lower()
    if op == "retweet":
        return reply_mod.make_retweet_target(target)
    if op == "reply":
        return reply_mod.make_reply_target(target)
    if op == "quote":
        return target
    return ""


# ─────────────────── "改文字" 状态 ───────────────────
#
# 用户在预览卡点「✏️ 改文字」后，机器人提示"请把改好的文字发给我"，
# 下一条文本消息即作为该任务的新正文。按 chat 记，避免多会话串台。

_edit_waiting: dict[int, int] = {}      # chat_id -> job_id


def _mark_pending_edit(chat_id: int, job_id: int) -> None:
    _edit_waiting[int(chat_id)] = int(job_id)


def _take_pending_edit(chat_id: int):
    """取出并**清除**该会话等待改文字的任务 id；没有则返回 None。"""
    return _edit_waiting.pop(int(chat_id), None)


async def _apply_edit(job_id: int, msg) -> None:
    """把用户新发的文字写进那条任务，并重新出预览卡。"""
    new_text = (msg.text or "").strip()
    row = queue.get(job_id)
    if row is None:
        await msg.reply_text(f"任务 #{job_id} 不存在，已取消改文字")
        return
    if row["status"] not in ("awaiting", "pending"):
        await msg.reply_text(f"任务 #{job_id} 当前状态为 {row['status']}，不能再改文字")
        return
    kind = row["kind"] or "text"
    old_qid = str(row["quote_id"] or "")
    if reply_mod.target_kind(old_qid):
        # 评论/引用/转帖的目标不能因为改文字而丢 —— 新正文里通常没有链接，
        # 若按普通逻辑重算会把 r:<id> / t:<id> / 纯数字引用抹成空，
        # 评论会变成孤儿帖、引用会丢掉被引推文、转帖会变成一条空推文。
        qid = old_qid
    else:
        qid = textutil.first_tweet_id(new_text) or ""
    h = textutil.content_hash(kind, new_text, qid, row["media_path"] or "")
    mark(job_id, row["status"], raw_text=new_text, quote_id=qid, content_hash=h)

    preview = compose(settings.effective_prefix(), new_text,
                      settings.effective_suffix())
    media = media_mod.parse_media_paths(row["media_path"])
    tag = f"（{len(media)} 图）" if media else ""
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 添加到队列", callback_data=f"ok:{job_id}"),
         InlineKeyboardButton("🚀 实时发送", callback_data=f"pub:{job_id}")],
        [InlineKeyboardButton("✏️ 修改文字", callback_data=f"edit:{job_id}"),
         InlineKeyboardButton("❌ 取消", callback_data=f"no:{job_id}")],
    ])
    await msg.reply_text(
        f"已更新 #{job_id}{tag}（{weighted_len(preview)}/280）\n\n{preview}",
        reply_markup=kb,
    )


# ─────────────────── 相册聚合实现 ───────────────────

async def _enqueue_retweet(msg, rstate: dict) -> None:
    """转帖：立刻入队，出确认卡。

    转帖没有正文、没有媒体，所以不需要再收任何输入 —— 点一下「🔄 转帖」
    就应该看到一张卡，而不是先跳去等文字。

    去重键用**链接那条消息的 id**（不是按钮消息 id）：同一条 update 被
    Telegram 重投时唯一键会拦住，不会转推两次。
    """
    chat_id = getattr(msg, "chat_id", 0)
    target = str(rstate.get("target") or "")
    quote_id = reply_mod.make_retweet_target(target)
    src = int(rstate.get("src") or 0) or int(getattr(msg, "message_id", 0) or 0)
    h = content_hash("retweet", "", quote_id, "")
    job_id = enqueue(
        tg_chat_id=chat_id, tg_msg_id=src, kind="text",
        raw_text="", media_path="", quote_id=quote_id, content_hash=h,
        status="awaiting" if MODE == "confirm" else "pending",
    )
    _take_reply(chat_id)
    if job_id is None:
        await msg.reply_text("这个转帖已经在队列里了（同一条消息重复投递）。")
        return
    if MODE != "confirm":
        await msg.reply_text(f"已入队 #{job_id} · 🔄 转帖 "
                             f"{reply_mod.tweet_url(target)}")
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 添加到队列", callback_data=f"ok:{job_id}"),
         InlineKeyboardButton("🚀 实时发送", callback_data=f"pub:{job_id}")],
        [InlineKeyboardButton("❌ 取消", callback_data=f"no:{job_id}")],
    ])
    try:
        sent_msg = await msg.reply_text(
            f"🔄 待确认 #{job_id} · 转帖\n{reply_mod.tweet_url(target)}\n\n"
            "转帖会用我的账号把这条推文转到我的主页（不带文字/图片）。",
            reply_markup=kb)
        try:
            note_card(chat_id, job_id, sent_msg.message_id)
        except Exception:
            pass
    except Exception:
        await msg.reply_text(f"🔄 待确认 #{job_id} · 转帖 {reply_mod.tweet_url(target)}",
                             reply_markup=kb)


def _media_group_id(msg) -> str:
    """取该消息的 media_group_id（相册消息才有）；不是相册返回空串。"""
    try:
        return str(getattr(msg, "media_group_id", "") or "")
    except Exception:
        return ""


async def _flush_album(group_id: str) -> None:
    """收集窗口到期：把攒下的相册消息合并成**一条**任务入队。"""
    entry = _albums.pop(group_id, None)
    if not entry:
        return
    msgs = entry.get("messages") or []
    if not msgs:
        return
    try:
        await _enqueue_album(msgs, quote_id=str(entry.get("quote_id") or ""),
                             op=str(entry.get("op") or ""))
    except Exception as e:
        log.exception("相册入队失败: %s", e)
        try:
            await msgs[0].reply_text(f"相册处理失败：{type(e).__name__}: {e}")
        except Exception:
            pass


async def _enqueue_album(msgs: list, *, quote_id: str = "", op: str = "") -> None:
    """把同一相册的多条消息合并成一条任务。

    规则：
      * 最多取 X 允许的 4 张（core.media.MAX_IMAGES），超出的明确告知用户
      * 正文取**第一条带 caption 的消息**（Telegram 只把 caption 挂在首条上）
      * 存成 JSON 数组（core.media.pack_media_paths），单图仍存文件名
      * `quote_id` 非空时目标由调用方锁定（图文评论 `r:<id>`）；空则按老规矩
        从正文里找推文链接当引用
    """
    msgs = sorted(msgs, key=lambda m: getattr(m, "message_id", 0))
    head = msgs[0]
    chat_id = getattr(head, "chat_id", 0)
    text = ""
    for m in msgs:
        cap = (getattr(m, "caption", "") or "").strip()
        if cap:
            text = cap
            break

    # 逐张下载（并发没必要：相册通常就 2~4 张，串行更稳、错误更好定位）
    saved: list[str] = []
    failed: list[str] = []
    keep = msgs[: media_mod.MAX_IMAGES]
    dropped = msgs[media_mod.MAX_IMAGES:]
    for i, m in enumerate(keep):
        kind = "video" if getattr(m, "video", None) else "photo"
        if getattr(m, "document", None) and not getattr(m, "photo", None):
            kind = "document"
        try:
            name = await _save_media(m, kind, slot=i if len(keep) > 1 else 0)
            saved.append(name)
        except Exception as e:
            failed.append(f"{type(e).__name__}: {e}")
            log.warning("相册第 %d 张下载失败：%s", i + 1, e)

    warnings: list[str] = []
    if dropped:
        warnings.append(f"X 单条最多 {media_mod.MAX_IMAGES} 张图，已忽略后 {len(dropped)} 张")
    if failed:
        warnings.append(f"{len(failed)} 张下载失败")

    kind = "photo"
    if saved:
        try:
            kind = media_mod.detect_kind(saved[0]) or "photo"
        except Exception:
            kind = "photo"

    # 图文评论的目标由流程锁定；普通图文才从正文里找引用链接
    if not quote_id:
        quote_id = textutil.first_tweet_id(text) or ""
    media_field = media_mod.pack_media_paths(saved)
    # 指纹里带上整套媒体，换图集就不会被误判重复
    h = textutil.content_hash(kind, text, quote_id, media_field)

    job_id = enqueue(
        tg_chat_id=chat_id, tg_msg_id=getattr(head, "message_id", 0),
        kind=kind, raw_text=text, media_path=media_field, quote_id=quote_id,
        content_hash=h,
        status="awaiting" if MODE == "confirm" else "pending",
    )
    if job_id is None:
        return

    _take_reply(chat_id)

    preview = compose(settings.effective_prefix(), text, settings.effective_suffix())
    warn = ("\n⚠ " + "；".join(warnings)) if warnings else ""
    header = f"🖼 待确认 #{job_id} · 图文（{len(saved)} 图）"
    tline = _target_line(quote_id)
    if tline:
        header += "\n" + tline
    if MODE == "confirm":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 添加到队列", callback_data=f"ok:{job_id}"),
             InlineKeyboardButton("🚀 实时发送", callback_data=f"pub:{job_id}")],
            [InlineKeyboardButton("✏️ 修改文字", callback_data=f"edit:{job_id}"),
             InlineKeyboardButton("❌ 取消", callback_data=f"no:{job_id}")],
        ])
        body = f"{header}（{weighted_len(preview)}/280）\n\n{preview}{warn}"
        try:
            sent_msg = await head.reply_text(body, reply_markup=kb)
        except Exception:
            sent_msg = await head.reply_text(f"{header}{warn}", reply_markup=kb)
        try:
            note_card(chat_id, job_id, sent_msg.message_id)
        except Exception:
            pass
    else:
        await head.reply_text(f"已入队 #{job_id} · {header.split('·')[0].strip()}"
                              f"（{len(saved)} 图），预计 {weighted_len(preview)}/280 字{warn}")


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await _on_message_inner(update, ctx)
    except Exception:
        # 绝不静默：以前这里没有兜底，异常会被 PTB 吞进 error handler，
        # 表现就是"用户发了消息，机器人像没收到一样"——极难排查。
        log.exception("收料处理异常（消息已收到但处理失败）")
        try:
            m = update.effective_message
            if m is not None:
                await m.reply_text("处理这条消息时出错了，请稍后重试（详情见日志）")
        except Exception:
            pass


async def _on_message_inner(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    # MessageHandler 现在不过滤 COMMAND（否则 `/sign` 会被 PTB 挡在外面
    # 到不了这里）。所以命令要在这一步显式跳过 —— 它们由 CommandHandler 处理。
    # 不跳的话，管理员发 `/status` 会被当成推文正文投料。
    if (getattr(msg, "text", "") or "").lstrip().startswith("/"):
        return
    # `/sign` 要在鉴权**之前**处理 —— 申请人本来就还不是管理员
    if msg is not None and await _maybe_sign_command(update, msg):
        return
    ok, why = await _authorized(update)
    if not ok:
        log.warning("拒收：%s", why)
        await _hint_not_admin(update, why)
        return
    # 用户可能先点了「📤 发送 -> 纯文字/图文」，这里把那个模式消费掉
    # （只影响提示与日志；即使没点过流程，直接发内容也照常处理，保持宽松）
    send_mode = _take_send_mode(update.effective_chat.id if update.effective_chat else 0)
    if send_mode:
        log.info("发送流程模式=%s", send_mode)
    log.info("收到消息 id=%s kind=%s group=%s text=%r",
             getattr(msg, "message_id", "?"),
             ("photo" if getattr(msg, "photo", None) else
              "video" if getattr(msg, "video", None) else
              "document" if getattr(msg, "document", None) else "text"),
             _media_group_id(msg),
             ((getattr(msg, "text", "") or getattr(msg, "caption", "") or "")[:40]))

    chat_id = update.effective_chat.id if update.effective_chat else 0

    # ── 数据日报文本命令（「日报」/「三月」/「单条 <ID>」）──
    # 只在用户**没走**「📤 发送」流程时才拦，免得把正文内容当成命令吃掉。
    _plain = (msg.text or "").strip()
    if _plain and (not send_mode or _plain.startswith("/")):
        if await _maybe_admin_command(msg, _plain):
            return
        if await _maybe_an_command(msg, _plain):
            return

    # ── 推文操作流程（转帖 / 引用 / 评论）──
    # 目标必须在这一步定下来，不能被消息正文里"碰巧出现"的链接顶替，
    # 也不能因为用户没走完流程就退化成普通发帖。
    quote_id = ""
    rstate = _peek_reply(chat_id)
    if rstate:
        stage = str(rstate.get("stage") or "")
        rmode = str(rstate.get("mode") or "text")
        if stage == "link":
            # 等链接：只有纯文字消息才认，图片消息没有链接可言
            if not msg.text or msg.photo or msg.video or msg.document:
                await msg.reply_text("请先发**那条推文的链接**（一条纯文字消息）。")
                return
            raw = (msg.text or "").strip()
            target = textutil.first_tweet_id(raw) or (raw if raw.isdigit() else "")
            if not target:
                await msg.reply_text(
                    "没认出推文链接。\n请发形如 "
                    "https://x.com/用户名/status/1234567890 的链接，"
                    "或直接发推文 ID。")
                return
            _set_reply_target(chat_id, target, getattr(msg, "message_id", 0))
            await _show_action_panel(msg, reply_mod.tweet_url(target))
            return

        if stage == "choose":
            # 已有目标，正等用户点操作。用户又发消息了 —— 说明他想换一条链接，
            # 那就把这条当新链接收下（比死等按钮友好，也更符合"直接粘链接"的用法）。
            if msg.text and not (msg.photo or msg.video or msg.document):
                raw = (msg.text or "").strip()
                target = textutil.first_tweet_id(raw) or (raw if raw.isdigit() else "")
                if target:
                    _set_reply_target(chat_id, target, getattr(msg, "message_id", 0))
                    await _show_action_panel(msg, reply_mod.tweet_url(target))
                    return
            await msg.reply_text("请点上面的按钮选：🔄 转帖 / ✏️ 引用 / 💬 评论")
            return

        # stage == "content"：已经知道目标 + 操作，等正文（评论的图文模式则等图片）
        rop = str(rstate.get("op") or "")
        quote_id = _reply_quote_id(rstate)
        if not quote_id:
            _take_reply(chat_id)
            await msg.reply_text("这条流程的目标丢了，请重新发一次推文链接。")
            return
        if rop == "retweet":
            # 转帖不需要任何正文：用户多发的消息是"等一下"还是"催一下"无从判断，
            # 绝不能把它当成推文内容静默发出去。
            _take_reply(chat_id)
            await msg.reply_text(
                "🔄 转帖不需要文字或图片，已取消这次输入。\n"
                f"要转帖的话请重新发链接：{reply_mod.tweet_url(rstate.get('target'))}")
            return
        if rop == "reply" and rmode == "text" and (msg.photo or msg.video or msg.document):
            await msg.reply_text(
                "这条评论选的是**只发文字**，请不要发图片。\n"
                "（想带图请重新发链接 → 选「💬 评论 → 🖼 图文」）")
            return

    # ── 谁在等"改文字"？── 把这条消息当作新正文，替换那条任务 ──
    edited = _take_pending_edit(update.effective_chat.id if update.effective_chat else 0)
    if edited is not None:
        if msg.text and not msg.photo and not msg.video and not msg.document:
            await _apply_edit(edited, msg)
            return

    # ── 用户直接粘一条推文链接 ──
    # 这里是本次交互改动的重点：以前粘链接=直接当引用转发发出去（机器人替用户
    # 猜了操作），现在**先问一句**要做什么。链接一般是用户发给机器人的"目标"，
    # 不是要发布的正文，所以只要整条消息就是一条链接/ID，就先拿它当目标。
    # ⚠ 用户刚点过「📤 发送 → 纯文字」，那这条链接就是**正文**，不是目标；
    #   只有没走发送流程的裸链接才拦下来问操作。
    if not (rstate or quote_id or send_mode) and msg.text \
            and not (msg.photo or msg.video or msg.document):
        raw_link = (msg.text or "").strip()
        bare_target = textutil.first_tweet_id(raw_link) or (
            raw_link if raw_link.isdigit() else "")
        # 只有"这条链接就是全部内容"才拦下来问。判据不能只看长度：
        # 「看看这条 https://… 说得对吗」长度也短，但那是**要发布的正文**。
        # 所以把链接本身抠掉，看还剩下什么 —— 只剩标点/空白才算裸链接。
        remainder = textutil.URL_RE.sub("", raw_link)
        remainder = remainder.strip(" \t\r\n，。！？、,.!?;；:：\"'“”‘’()（）[]【】-—…")
        if bare_target and not remainder:
            _mark_reply(chat_id, target=bare_target, stage="choose",
                        src=getattr(msg, "message_id", 0))
            await _show_action_panel(msg, reply_mod.tweet_url(bare_target))
            return

    # ── 相册消息：先攒起来，窗口关闭后合并成一条任务 ──
    gid = _media_group_id(msg)
    if gid and (msg.photo or msg.video or msg.document):
        entry = _albums.get(gid)
        if entry is None:
            # 评论的图文模式也会走相册聚合，目标必须随相册一起带过去，
            # 否则聚合出的任务会丢掉"对哪条推文动手"这个关键信息。
            entry = {"messages": [], "quote_id": quote_id, "op": str(rstate.get("op") or "")}
            _albums[gid] = entry
        elif quote_id:
            entry["quote_id"] = quote_id
            entry["op"] = str(rstate.get("op") or "")
        # 只有第一条负责调度"窗口关闭后收口"
        async def _later(g=gid):
            await asyncio.sleep(ALBUM_WINDOW_SECONDS)
            await _flush_album(g)
        try:
            asyncio.get_running_loop().create_task(_later())
        except Exception:
            pass
        entry["messages"].append(msg)
        return

    if msg.photo:
        kind, text = "photo", (msg.caption or "")
    elif msg.video:
        kind, text = "video", (msg.caption or "")
    elif msg.document:
        kind, text = "document", (msg.caption or "")
    elif msg.text:
        kind, text = "text", msg.text
    else:
        await msg.reply_text("暂支持：文本 / 图片 / 视频 / 文件（带说明文字）")
        return

    media_path = ""
    media_failed = ""
    if kind != "text":
        try:
            media_path = await _save_media(msg, kind)
        except Exception as e:  # 文件过大(>20MB)等
            media_failed = str(e)
            log.warning("媒体下载失败：%s", e)

    # 目标由上面的流程锁定（评论/引用）；否则才从正文里找推文链接当引用。
    # ⚠ 转帖在这一步之前就返回了，不会走到这里 —— 转帖没有正文。
    if not quote_id:
        quote_id = first_tweet_id(text) or ""
    # ⚠ 必须把媒体文件名算进指纹：否则"两张不同的图 + 同样文字"会被
    #   内容去重误判为重复（同一条素材被反复转发才该拦）。
    h = content_hash(kind, text, quote_id, media_path or "")

    # 去重窗口取运行期设置（用户能在控制台改），不是启动时的静态值
    if is_dup_content(h, settings.effective_dedup_window()) and not media_path:
        enqueue(
            tg_chat_id=msg.chat_id, tg_msg_id=msg.message_id, kind=kind,
            raw_text=text, content_hash=h, status="duplicate",
        )
        await msg.reply_text("内容与最近已发的一条重复，已忽略")
        return

    job_id = enqueue(
        tg_chat_id=msg.chat_id, tg_msg_id=msg.message_id, kind=kind,
        raw_text=text, media_path=media_path, quote_id=quote_id, content_hash=h,
        status="awaiting" if MODE == "confirm" else "pending",
    )
    if job_id is None:
        return  # update 重投，已入队

    # 推文操作的内容已入队，结束这次的流程状态
    if rstate:
        _take_reply(chat_id)

    preview = compose(settings.effective_prefix(), text, settings.effective_suffix())
    warn = f"\n⚠ 媒体未取到（将只发文字）：{media_failed}" if media_failed else ""
    if MODE == "confirm":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 添加到队列", callback_data=f"ok:{job_id}"),
             InlineKeyboardButton("🚀 实时发送", callback_data=f"pub:{job_id}")],
            [InlineKeyboardButton("✏️ 修改文字", callback_data=f"edit:{job_id}"),
             InlineKeyboardButton("❌ 取消", callback_data=f"no:{job_id}")],
        ])
        icon = {"photo": "🖼", "video": "🎬", "document": "📄"}.get(kind, "📝")
        kind_tag = ""
        if media_path:
            n = len(media_mod.parse_media_paths(media_path)) or 1
            kind_tag = f" · {'图文' if text.strip() else '纯图'}（{n} 图）"
        reply_tag = _target_line(quote_id)
        reply_tag = f"\n{reply_tag}" if reply_tag else ""
        sent_msg = await msg.reply_text(
            f"{icon} 待确认 #{job_id}{kind_tag}"
            f"（{weighted_len(preview)}/280）{reply_tag}\n\n{preview}{warn}",
            reply_markup=kb,
        )
        # 登记卡片：发出后好把它删掉，保持聊天干净
        try:
            note_card(msg.chat_id, job_id, sent_msg.message_id)
        except Exception:
            pass
    else:
        await msg.reply_text(f"已入队 #{job_id}，预计 {weighted_len(preview)}/280 字")


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """所有内联按钮的总入口。

    data 形如 `action:arg`。action 一览：
      ok:#      放行这条（⚠️ 会真的发到 X —— 用户点按钮即为明确授权）
      no:#      取消这条
      edit:#    进入"改文字"，等下一条文本消息
      preview:# 展开预览
      pub:#     单条立即发布（发这条）
      retry:#   重试失败的那条
      menu      回主菜单
      queue     打开队列
      status    看状态
      pause / resume   暂停 / 恢复发布
      backends  后端选择
      setbe:x   切到后端 x
    """
    q = update.callback_query
    if q is None:
        return
    await q.answer()
    data = q.data or ""
    action, _, arg = data.partition(":")

    # ── 发送流程 ──
    if action == "send":
        await _show_send(q.message)
        return
    if action == "sendtext":
        _mark_send_mode(q.message.chat.id, "text")
        await _prompt_send(q.message, "text")
        return
    if action == "sendmedia":
        _mark_send_mode(q.message.chat.id, "media")
        await _prompt_send(q.message, "media")
        return
    if action == "sendreply":
        # 入口就是"先要链接"：操作要到链接之后才问（转帖/引用/评论）
        _mark_reply(q.message.chat.id)
        await q.message.reply_text(
            "🔁 转帖 / 引用 / 评论\n\n"
            "先把**那条推文的链接**发给我。\n"
            "（也可以直接发推文 ID；发完我会问你要做哪种）")
        return

    # ── 推文操作流程（转帖 / 引用 / 评论）──
    chat_id = q.message.chat.id if q.message and q.message.chat else 0
    if action == "op":
        st = _peek_reply(chat_id)
        if not st or not st.get("target"):
            await q.message.reply_text("目标丢了，请重新发那条推文的链接。")
            return
        if arg == "retweet":
            # 转帖没有正文、没有媒体，直接出任务 —— 多问一步没有信息量
            _set_reply_op(chat_id, "retweet")
            await _enqueue_retweet(q.message, st)
            return
        if arg == "quote":
            _set_reply_op(chat_id, "quote")
            await q.message.reply_text(
                f"✏️ 引用 {reply_mod.tweet_url(st.get('target'))}\n\n"
                "把**要配的话**发给我（也可以只发图片，可多选，最多 4 张）。")
            return
        if arg == "reply":
            await _show_reply_mode_panel(q.message)
            return
        if arg == "back":
            await _show_action_panel(q.message, reply_mod.tweet_url(st.get("target")))
            return
        if arg == "cancel":
            _take_reply(chat_id)
            await q.message.reply_text("已取消这次的推文操作。")
            return
    if action == "opreply":
        st = _peek_reply(chat_id)
        if not st or not st.get("target"):
            await q.message.reply_text("目标丢了，请重新发那条推文的链接。")
            return
        if arg == "media":
            _set_reply_op(chat_id, "reply", "media")
            await q.message.reply_text(
                f"💬 图文评论 {reply_mod.tweet_url(st.get('target'))}\n\n"
                "把**图片**发给我（可多选，最多 4 张），"
                "并在**同一条消息**里写好评论文字。")
        else:
            _set_reply_op(chat_id, "reply", "text")
            await q.message.reply_text(
                f"💬 纯文字评论 {reply_mod.tweet_url(st.get('target'))}\n\n"
                "把**评论文字**发给我。")
        return
    if action == "settings":
        await _show_settings(q.message)
        return
    if action == "toggle_queue_send":
        cur = settings.queue_send_enabled()
        settings.set_many({"queue_send_enabled": "0" if cur else "1"})
        await _show_settings(q.message)
        return
    if action == "set_interval":
        await _show_interval_picker(q.message)
        return
    if action == "admins":
        await _show_admins(q.message)
        return
    if action == "admin_here":
        uid = q.from_user.id if q.from_user else 0
        if not uid:
            await q.message.reply_text("拿不到你的 user id，请在私聊里操作")
            return
        try:
            from core import tg_admins
        except Exception as e:
            await q.message.reply_text(f"管理员模块不可用：{type(e).__name__}")
            return
        tg_admins.ensure_seed()
        u = q.from_user
        if tg_admins.add_admin(uid, getattr(u, "username", "") or "",
                               getattr(u, "full_name", "") or "",
                               added_by=uid, source="manual"):
            await q.message.reply_text(f"✅ 已把 {uid} 加进管理员列表")
        else:
            await q.message.reply_text("❌ 加入失败")
        await _show_admins(q.message)
        return
    if action == "sg":
        # 审批管理员申请 —— **只有所有者**能点
        try:
            from core import tg_admins
        except Exception as e:
            await q.message.reply_text(f"管理员模块不可用：{type(e).__name__}")
            return
        uid = q.from_user.id if q.from_user else 0
        if not tg_admins.is_owner(uid):
            await q.message.reply_text("这条申请只有所有者能审批。")
            return
        kind, _, sid = (arg or "").partition(":")
        try:
            sid_i = int(sid)
        except Exception:
            await q.message.reply_text("申请编号不对")
            return
        approve = (kind == "ok")
        ok, note, row = tg_admins.decide(sid_i, approve, decided_by=uid)
        if not ok:
            await q.message.reply_text(f"⚠️ {note}")
            return
        # 通知申请人（他刚发过 /sign，所以机器人能给他发私信）
        if row:
            try:
                await q.get_bot().send_message(
                    row["user_id"],
                    ("✅ 你的申请已通过，现在可以给机器人发料了。"
                     if approve else
                     "❌ 你的申请未通过。一个月后可以重新申请。"))
            except Exception as e:
                log.warning("通知申请人失败（%s）", type(e).__name__)
        # 把所有者那条消息钉上结果，防止手滑点两次
        try:
            await q.edit_message_text(
                (q.message.text or "") +
                f"\n\n{'✅ 已批准' if approve else '❌ 已拒绝'}")
        except Exception:
            try:
                await q.message.reply_text("✅ 已批准" if approve else "❌ 已拒绝")
            except Exception:
                pass
        return

    # ── 无需参数的菜单动作 ──
    if action == "menu":
        await _show_menu(q.message)
        return
    if action == "queue":
        await _show_queue(q.message)
        return
    if action == "status":
        await _send_status(q.message)
        return
    if action == "pause":
        settings.set_many({"paused": "1"})
        await q.message.reply_text("⏸ 已暂停发布（队列保留，恢复后继续）")
        await _show_menu(q.message)
        return
    if action == "resume":
        settings.set_many({"paused": "0"})
        await q.message.reply_text("▶️ 已恢复发布")
        await _show_menu(q.message)
        return
    if action == "backends":
        await _show_backends(q.message)
        return
    if action == "an":
        await _an_send_summary(q.message)
        return
    if action == "anr":
        await _an_refresh(q.message)
        return
    if action == "anl":
        await _an_show_tweets(q.message, (arg or "").strip())
        return
    if action == "anp":
        day, _, pg = (arg or "").partition(":")
        try:
            page = int(pg)
        except Exception:
            page = 0
        await _an_show_tweets(q.message, day.strip(), page)
        return
    if action == "ant":
        await _an_show_tweet(q.message, (arg or "").strip())
        return
    if action == "rv":
        # 采集内容的审核按钮：rv:ok:<key> / rv:no:<key>
        kind, _, key = (arg or "").partition(":")
        if not key:
            await q.message.reply_text("这条没有 key，处理不了")
            return
        uid = q.from_user.id if q.from_user else 0
        try:
            from core import tg_admins
            if not tg_admins.is_admin(uid):
                await q.message.reply_text("只有管理员能审核")
                return
        except Exception:
            pass
        await _review_decide(q, key, kind == "ok")
        return
    if action == "setbe":
        await _switch_backend(q.message, arg)
        return

    if action == "interval":
        try:
            mins = max(0, int(arg))
        except Exception:
            mins = 0
        settings.set_many({"send_interval_minutes": str(mins)})
        await q.message.reply_text(
            f"✅ 发送间隔已设为 {'不限制' if mins == 0 else str(mins) + ' 分钟'}")
        await _show_settings(q.message)
        return

    # ── 需要任务 id 的动作 ──
    if not arg.isdigit():
        return
    job_id = int(arg)

    if action == "ok":
        mark(job_id, "pending")
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await q.message.reply_text(f"✅ #{job_id} 已放行，排队发布中"
                                   f"（发出后这条预览卡会自动删除）")
    elif action == "no":
        mark(job_id, "canceled")
        chat_id = q.message.chat.id if q.message and q.message.chat else 0
        forget_card(chat_id, job_id)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        # 取消后把卡片删掉，聊天保持干净
        try:
            if q.message is not None:
                await q.message.delete()
        except Exception:
            pass
        await q.message.reply_text(f"#{job_id} 已取消")
    elif action == "edit":
        row = queue.get(job_id)
        if row is None:
            await q.message.reply_text(f"#{job_id} 不存在")
            return
        chat_id = q.message.chat.id if q.message and q.message.chat else 0
        _mark_pending_edit(chat_id, job_id)
        await q.message.reply_text(
            f"✏️ 请把 #{job_id} 的新正文发给我（只发文字即可，图片不用重发）")
    elif action == "preview":
        await _send_preview(q.message, job_id)
    elif action == "pub":
        # 用户点按钮 = 明确要发，直接转 pending 并立即发布这一条
        row = queue.get(job_id)
        if row is None:
            await q.message.reply_text(f"#{job_id} 不存在")
            return
        if row["status"] not in ("awaiting", "failed", "canceled", "pending"):
            await q.message.reply_text(f"#{job_id} 当前状态 {row['status']}，不可发布")
            return
        mark(job_id, "pending")
        pipe = get_pipeline()
        if pipe is None:
            # 用户已经明确点了「🚀 发送」，就不能把任务丢回队列干等：
            # 若 queue_send_enabled=0（只手动发），主循环永远跳过它，任务会
            # 永久卡在 pending。这里兜底建一个（带 TG 回执）的 pipeline，
            # 把这条真正发出去。
            try:
                pipe = ensure_pipeline(getattr(ctx, "application", None))
            except Exception as e:
                log.exception("#%s 兜底创建 pipeline 失败", job_id)
                await q.message.reply_text(
                    f"❌ #{job_id} 无法发布：发布模块不可用（{type(e).__name__}: {e}）")
                return
        await q.message.reply_text(f"🚀 #{job_id} 正在发布…")
        try:
            res = await pipe.publish_one(job_id)
            if getattr(res, "ok", False):
                await q.message.reply_text(
                    f"✅ #{job_id} 已发布\n{getattr(res, 'tweet_url', '') or ''}")
            else:
                await q.message.reply_text(
                    f"❌ #{job_id} 发布失败\n{(getattr(res, 'error', '') or '')[:300]}")
        except Exception as e:
            await q.message.reply_text(f"发布异常：{type(e).__name__}: {e}")
    elif action == "retry":
        if queue.requeue(job_id):
            await q.message.reply_text(f"🔄 #{job_id} 已重新排队")
        else:
            await q.message.reply_text(f"#{job_id} 当前状态不可重试")


def _require_admin(handler):
    """把命令处理函数包一层鉴权。

    为什么需要：PTB 的 ``CommandHandler`` 会**先于** ``on_message`` 触发，
    所以非管理员发 ``/status``、``/queue`` 这类命令会绕过 ``_authorized()``
    直接拿到菜单/队列内容。这里统一在入口挡掉。

    ``/sign`` 是唯一对所有人开放的（它就是申请入口）。
    """
    @functools.wraps(handler)
    async def _wrapped(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        ok, why = await _authorized(update)
        if not ok:
            log.warning("拒收命令：%s", why)
            await _hint_not_admin(update, why)
            return
        return await handler(update, ctx)
    return _wrapped


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    # chat_id 对排障/配白名单很有用，保留显示
    await update.effective_message.reply_text(
        f"👋 chat_id = {chat.id} · 模式 = {MODE}")
    await _show_menu(update.effective_message)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_status(update.effective_message)


async def cmd_sign(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """`/sign` —— 申请使用权限（对所有人开放）。"""
    msg = update.effective_message
    if msg is not None:
        await _handle_sign(update, msg)
    if DRY_RUN:
        await update.effective_message.reply_text("（当前是 DRY-RUN，只打印不真发）")


async def cmd_queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_queue(update.effective_message)


async def cmd_retry(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    n = queue.reset_failed()
    await update.effective_message.reply_text(f"🔄 已重置 {n} 条失败任务")


async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_menu(update.effective_message)


async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    settings.set_many({"paused": "1"})
    await update.effective_message.reply_text("⏸ 已暂停发布（队列保留，恢复后继续）")


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    settings.set_many({"paused": "0"})
    await update.effective_message.reply_text("▶️ 已恢复发布")


async def cmd_backend(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_backends(update.effective_message)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("handler 异常", exc_info=ctx.error)


# ─────────────────────────── 发布循环接线 ───────────────────────────

def _make_notifier(app: Application | None):
    """有 Telegram bot 时同时发日志与回执；无 bot（Web-only）时退化为日志。"""
    if app is None or getattr(app, "bot", None) is None:
        return LogNotifier()
    return TelegramNotifier(app.bot)


async def _dry_loop() -> None:
    """干跑：只**读**队列里的 pending 打印，不 claim、不 mark（不改数据库状态）。"""
    log.info("[DRY-RUN] 只读模式：打印待发内容，不消耗任务、不改库状态")
    seen: set[int] = set()
    while True:
        try:
            for r in reversed(queue.recent(limit=50, status="pending")):
                if r["id"] in seen:
                    continue
                seen.add(r["id"])
                text = r["tweet_text"] or compose(
                    settings.effective_prefix(), r["raw_text"] or "", settings.effective_suffix()
                )
                log.info(
                    "[DRY-RUN] #%s kind=%s media=%s quote=%s attempts=%s len=%s\n----\n%s\n----",
                    r["id"], r["kind"], r["media_path"] or "-", r["quote_id"] or "-",
                    r["attempts"], weighted_len(text), text,
                )
            await asyncio.sleep(POLL_SECONDS)
        except asyncio.CancelledError:
            log.info("[DRY-RUN] 已停止")
            raise
        except Exception:
            log.exception("[DRY-RUN] 循环异常")
            await asyncio.sleep(10)


async def post_init(app: Application) -> None:
    """轮询启动后拉起后台任务：正式跑 pipeline，干跑走只读循环。"""
    global _pipeline
    if DRY_RUN:
        log.info("DRY-RUN 启用：不启动 pipeline（不发推、不动队列状态）")
        _tasks.append(asyncio.create_task(_dry_loop()))
        return
    _pipeline = Pipeline(notifier=_make_notifier(app))
    _tasks.append(asyncio.create_task(_pipeline.run_forever()))


async def post_shutdown(app: Application) -> None:
    """优雅收尾：先请求 pipeline 停止，再取消未结束的后台任务。"""
    global _pipeline
    if _pipeline is not None:
        _pipeline.stop()
    for t in _tasks:
        if not t.done():
            t.cancel()
    _tasks.clear()
    log.info("后台任务已收尾")


# ─────────────────────────── 入口 ───────────────────────────

def build_application(token: str | None = None) -> Application:
    """构造并返回 Application（**不启动轮询**），供 bot.py 与统一启动器（start.py）复用。

    handler 注册与 post_init/post_shutdown 都挂在这里，只此一份。
    调用方负责： initialize() / start() / updater.start_polling() / run_polling()。

    token 为 None 时按 settings.get("tg_token") → config.TG_TOKEN 顺序取
    （控制台里配的 token 优先于 .env，改完无需重启进程即可生效）。
    **向后兼容**：不传参时行为与以前完全一致。
    """
    tok = (token or "").strip()
    if not tok:
        try:
            tok = (settings.get("tg_token", "") or "").strip()      # 运行期配置优先
        except Exception:
            tok = ""
    if not tok:
        tok = TG_TOKEN                                              # 回落 .env / 环境变量
    if not tok:
        tok = require_tg()          # 都没有：与旧行为一致的清晰报错（SystemExit）

    app = (
        Application.builder()
        .token(tok)
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    # group=-1：只统计收到多少 update，不消费更新（后面的业务 handler 照常触发）
    for _t in _update_count_types():
        try:
            app.add_handler(TypeHandler(_t, on_any_update), group=-1)
        except Exception as e:
            log.debug("统计 handler 注册失败（不影响收料）：%s", e)
    app.add_handler(CommandHandler("start", _require_admin(cmd_start)))
    app.add_handler(CommandHandler("help", _require_admin(cmd_start)))
    # `/sign` 申请：**唯一**对所有人开放的命令（它就是申请入口）
    app.add_handler(CommandHandler("sign", cmd_sign))
    # 其余命令都要是管理员才能用
    app.add_handler(CommandHandler("menu", _require_admin(cmd_menu)))
    app.add_handler(CommandHandler("status", _require_admin(cmd_status)))
    app.add_handler(CommandHandler("queue", _require_admin(cmd_queue)))
    app.add_handler(CommandHandler("retry", _require_admin(cmd_retry)))
    app.add_handler(CommandHandler("pause", _require_admin(cmd_pause)))
    app.add_handler(CommandHandler("resume", _require_admin(cmd_resume)))
    app.add_handler(CommandHandler("backend", _require_admin(cmd_backend)))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(
        filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL,
        on_message))
    app.add_error_handler(on_error)
    return app


# ══════════════════════════════════════════════════════════
# 采集内容 · 人工审核（每小时推 1 条，通过才发）
# ══════════════════════════════════════════════════════════

def _review_module():
    try:
        from core.collect import review as _r
        return _r
    except Exception:
        return None


def _review_caption(item, comments_text: str = "") -> str:
    """审核卡片的文字（图走 photo 的 caption，限 1024 字）。"""
    plat = {"xiaohongshu": "小红书", "bilibili": "B站",
            "weibo": "微博", "tieba": "百度贴吧"}.get(item.get("platform"), item.get("platform"))
    head = (f"📥 待审核 · {plat}\n"
            f"♥{item.get('likes', 0)}  💬{item.get('comments', 0)}  "
            f"打分 {item.get('score', 0)}\n")
    title = (item.get("title") or "").strip()
    body = f"\n「{title[:60]}」\n" if title else ""
    tail = f"\n💬 热评\n{comments_text}\n" if comments_text else ""
    return (head + body + tail +
            "\n通过后才会发到 X")[:1000]


async def review_push_next(app) -> tuple[bool, str]:
    """挑一条待审推给管理员。**绝不抛异常**。"""
    rv = _review_module()
    if rv is None:
        return False, "审核模块不可用"
    try:
        if not rv.due():
            return False, "还没到推送时间"
        item = rv.next_candidate()
        if not item:
            return False, "没有够格的待审内容"
        try:
            from core import tg_admins
            chat = tg_admins.owner_id()
        except Exception:
            chat = 0
        if not chat:
            return False, "没有管理员（不知道推给谁）"

        # 热评：B站免登录直接拿，其他平台尽力
        ctext = ""
        try:
            from core.collect import comments as _c
            cs = _c.fetch_for(item, limit=_c.MAX_COMMENTS)
            ctext = _c.to_text(cs)
        except Exception:
            pass

        key = item["key"]
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ 通过并发送", callback_data=f"rv:ok:{key}"),
            InlineKeyboardButton("⏭ 跳过", callback_data=f"rv:no:{key}"),
        ]])
        img = (item.get("images") or [None])[0] or item.get("cover")
        cap = _review_caption(item, ctext)
        sent = None
        if img and app is not None:
            try:
                sent = await app.bot.send_photo(chat, photo=img, caption=cap,
                                                reply_markup=kb)
            except Exception as e:
                log.debug("发图失败，退回纯文字: %s", type(e).__name__)
        if sent is None and app is not None:
            sent = await app.bot.send_message(chat, cap, reply_markup=kb)
        mid = getattr(sent, "message_id", 0) if sent else 0
        rv.push_mark(key, chat_id=chat, message_id=mid)
        return True, f"已推送 #{key}"
    except Exception as e:
        log.warning("推送待审失败: %s", type(e).__name__)
        return False, f"推送异常：{type(e).__name__}"


async def _review_decide(q, key: str, approved: bool) -> None:
    """管理员点了按钮。通过 → 立刻发到 X。"""
    rv = _review_module()
    if rv is None:
        await q.message.reply_text("审核模块不可用")
        return
    from core.collect import store as _cs
    item = _cs.get_item(key)
    if not item:
        await q.message.reply_text("这条采集记录找不到了")
        return
    if not approved:
        rv.decide(key, approved=False)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await q.message.reply_text("⏭ 已跳过这条")
        return
    # 通过 → 发到 X（进现有发布队列，默认 pending = 会被发出去）
    ok, msg, jid = _cs.publish_item(key, text="", with_source=True, immediately=True)
    rv.decide(key, approved=bool(ok), job_id=jid)
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await q.message.reply_text(("✅ 已通过，" + msg) if ok else ("❌ 发布失败：" + msg))


def raise_keyboard_interrupt() -> None:
    """把 SIGTERM 转成 KeyboardInterrupt，让 run_polling 走正常收尾流程。"""
    raise KeyboardInterrupt


def main() -> None:
    global DRY_RUN
    setup_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="不入 X/浏览器，只打印将要发布的内容")
    ap.add_argument("--status", action="store_true", help="打印队列与配额统计后退出")
    args = ap.parse_args()
    DRY_RUN = args.dry_run

    init_db()
    settings.all_settings()          # 触发 settings 表建立

    if args.status:
        st = queue_stats()
        for k, v in sorted(st["by_status"].items()):
            print(f"  {k:<10} {v}")
        print(f"待发(pending): {queue_depth()}")
        print(f"本月已发: {month_sent_count()}/{settings.effective_monthly_limit()}")
        print(f"当前后端: {settings.current_backend()}  暂停: {settings.is_paused()}")
        print(f"数据库:   {DB_PATH}")
        print(f"媒体目录: {MEDIA_DIR}")
        return

    lock = single_instance_lock()
    if lock is None:
        raise SystemExit(
            "[lock] 已有实例在运行（或上次异常退出残留锁）。\n"
            f"       锁文件: {DATA_DIR / 'bot.lock'}\n"
            "       确认无其他进程后删除该文件再启动。"
        )

    log.info("配置就绪：模式=%s 干跑=%s 后端=%s 数据目录=%s",
             MODE, DRY_RUN, settings.current_backend(), DATA_DIR)

    app = build_application()

    # Windows 没有 SIGTERM/SIGHUP；Ctrl+C 由 PTB 自己处理
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, lambda *_: raise_keyboard_interrupt())
        except (ValueError, OSError):
            pass

    log.info("启动长轮询……")
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
    finally:
        try:
            lock.close()
        except Exception:
            pass
        log.info("已退出")


if __name__ == "__main__":
    main()
