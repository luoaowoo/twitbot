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

from core import config, media as media_mod, queue, settings, textutil
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
_pipeline: Pipeline | None = None
_tasks: list[asyncio.Task] = []


def get_pipeline() -> Pipeline | None:
    """当前进程的 Pipeline 实例（供 Web 控制台/统一启动器取运行态快照）。"""
    return _pipeline


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
    """运行期生效的 user 白名单：settings 非空优先，为空回落 config.ALLOWED_USERS。

    走运行期读取（不是模块级常量）—— 控制台改完白名单**无需重启进程**即生效。
    """
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
            return False, f"发送者 {uid} 不在 ALLOWED_USERS 白名单内"
    return True, ""


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
        [InlineKeyboardButton("⬅️ 返回", callback_data="menu")],
    ])
    await target.reply_text(
        "📤 发送内容\n\n请选择类型：\n"
        "· 纯文字 —— 只发文字\n"
        "· 图文 —— 图片 + 配文（最多 4 张）",
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
    """管理员 ID 面板（白名单）。"""
    users = sorted(settings.get("tg_allowed_users", "").split(",")) if settings.get("tg_allowed_users", "") else []
    users = [u.strip() for u in users if u.strip()]
    if users:
        cur = "\n".join(f"  · {u}" for u in users)
    else:
        cur = "  （未设置 —— 任何人搜到机器人都能发料 ⚠️）"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ 把当前会话设为管理员",
                              callback_data="admin_here")],
        [InlineKeyboardButton("⬅️ 返回", callback_data="backends")],
    ])
    await target.reply_text(
        f"👤 管理员 ID\n\n{cur}\n\n"
        f"只有列表里的 user id 能给机器人发料。",
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
        [InlineKeyboardButton("▶️ 恢复发布" if paused else "⏸ 暂停发布",
                              callback_data="resume" if paused else "pause")],
    ])
    text = ("🤖 TwitBot\n"
            f"{_status_line()}\n\n"
            "· 📤 发送 —— 选「纯文字 / 图文」，然后发内容\n"
            "· 📋 队列 —— 逐条预览 / 发送 / 修改\n"
            "· ⚙️ 设置 —— 队列发送开关、发送间隔")
    try:
        await target.reply_text(text, reply_markup=kb)
    except Exception:
        await target.reply_text(text, reply_markup=kb)


def _job_tag(row) -> str:
    """一行的图标 + 类型说明。"""
    media = media_mod.parse_media_paths(row["media_path"] if "media_path" in row.keys() else "")
    if media:
        n = len(media)
        return f"🖼 {n}图" + ("+字" if (row["raw_text"] or "").strip() else "")
    return "📝 纯文字"


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
            kb_rows.append([
                InlineKeyboardButton("👁 预览", callback_data=f"preview:{jid}"),
                InlineKeyboardButton("🚀 发送", callback_data=f"pub:{jid}"),
                InlineKeyboardButton("✏️ 修改", callback_data=f"edit:{jid}"),
                InlineKeyboardButton("❌ 删除", callback_data=f"no:{jid}"),
            ])
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
    if media:
        tail += f"\n图片：{len(media)} 张"
    if r["error"]:
        tail += f"\n⚠️ 上次失败：{str(r['error'])[:150]}"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🚀 发送", callback_data=f"pub:{job_id}"),
        InlineKeyboardButton("✏️ 修改文字", callback_data=f"edit:{job_id}"),
        InlineKeyboardButton("❌ 删除", callback_data=f"no:{job_id}"),
    ]])
    await target.reply_text(f"{head}\n\n{body or '（无文字）'}{tail}", reply_markup=kb)


async def _send_status(target) -> None:
    st = queue.stats()
    by = "  ".join(f"{k}={v}" for k, v in (st.get("by_status") or {}).items()) or "空"
    await target.reply_text(
        f"📊 状态\n\n队列：{by}\n"
        f"本月已发：{st.get('month_sent', 0)}/{st.get('limit', 0)}\n"
        f"后端：{settings.current_backend()}"
        f"{'（已暂停）' if settings.is_paused() else ''}")


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
    qid = textutil.first_tweet_id(new_text) or ""
    kind = row["kind"] or "text"
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
        await _enqueue_album(msgs)
    except Exception as e:
        log.exception("相册入队失败: %s", e)
        try:
            await msgs[0].reply_text(f"相册处理失败：{type(e).__name__}: {e}")
        except Exception:
            pass


async def _enqueue_album(msgs: list) -> None:
    """把同一相册的多条消息合并成一条任务。

    规则：
      * 最多取 X 允许的 4 张（core.media.MAX_IMAGES），超出的明确告知用户
      * 正文取**第一条带 caption 的消息**（Telegram 只把 caption 挂在首条上）
      * 存成 JSON 数组（core.media.pack_media_paths），单图仍存文件名
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

    preview = compose(settings.effective_prefix(), text, settings.effective_suffix())
    warn = ("\n⚠ " + "；".join(warnings)) if warnings else ""
    header = f"🖼 待确认 #{job_id} · 图文（{len(saved)} 图）"
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
    ok, why = await _authorized(update)
    if not ok:
        log.warning("拒收：%s", why)
        return
    msg = update.effective_message
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

    # ── 谁在等"改文字"？── 把这条消息当作新正文，替换那条任务 ──
    edited = _take_pending_edit(update.effective_chat.id if update.effective_chat else 0)
    if edited is not None:
        if msg.text and not msg.photo and not msg.video and not msg.document:
            await _apply_edit(edited, msg)
            return

    # ── 相册消息：先攒起来，窗口关闭后合并成一条任务 ──
    gid = _media_group_id(msg)
    if gid and (msg.photo or msg.video or msg.document):
        entry = _albums.get(gid)
        if entry is None:
            entry = {"messages": []}
            _albums[gid] = entry
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
        sent_msg = await msg.reply_text(
            f"{icon} 待确认 #{job_id}{kind_tag}"
            f"（{weighted_len(preview)}/280）\n\n{preview}{warn}",
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
        cid = q.message.chat.id if q.message and q.message.chat else 0
        uid = q.from_user.id if q.from_user else 0
        if not uid:
            await q.message.reply_text("拿不到你的 user id，请在私聊里操作")
            return
        cur = settings.get("tg_allowed_users", "")
        ids = {x.strip() for x in (cur or "").split(",") if x.strip()}
        ids.add(str(uid))
        settings.set_many({"tg_allowed_users": ",".join(sorted(ids))})
        await q.message.reply_text(f"✅ 已把 {uid} 加入管理员白名单")
        await _show_admins(q.message)
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
            await q.message.reply_text(f"#{job_id} 已放行（发布循环不在本进程，稍后自动发出）")
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


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    # chat_id 对排障/配白名单很有用，保留显示
    await update.effective_message.reply_text(
        f"👋 chat_id = {chat.id} · 模式 = {MODE}")
    await _show_menu(update.effective_message)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_status(update.effective_message)
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
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("retry", cmd_retry))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("backend", cmd_backend))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL)
        & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)
    return app


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
