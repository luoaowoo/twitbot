"""回执通知抽象（★A）—— 发布成败后怎么告诉人。

三种实现：
  * `LogNotifier`      —— 只写日志，永远可用（默认，也是无 Telegram 场景的兜底）
  * `TelegramNotifier` —— 回执发回原消息；没传 bot 时静默跳过（Web-only 部署）
  * `MultiNotifier`    —— 组合多个；单个失败不影响其它

**硬性要求**：任何通知失败都不得影响发布流程 —— 各实现内部自吞异常，
`MultiNotifier` 逐项兜底，`Pipeline` 再兜一层。
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Protocol, runtime_checkable

log = logging.getLogger("twitbot.notifier")


@runtime_checkable
class Notifier(Protocol):
    """回执通知契约。两个方法都必须是 async 且**不抛异常**。"""

    async def sent(self, job, result) -> None:
        """发布成功回执。"""
        ...

    async def failed(self, job, result) -> None:
        """发布失败回执。"""
        ...


# ── 正文渲染（纯函数，便于测试）────────────────────────────

def sent_text(job, result) -> str:
    url = result.tweet_url or (f"https://x.com/i/status/{result.tweet_id}"
                               if result.tweet_id else "")
    parts = [f"✅ 已发布 #{job.id}"]
    if url:
        parts.append(url)
    if result.backend:
        parts.append(f"后端：{result.backend}")
    if result.degraded:
        parts.append(f"⚠ 降级：{result.degraded}")
    body = (result.text or job.tweet_text or job.raw_text or "").strip()
    if body:
        parts.append("")
        parts.append(body[:600])
    return "\n".join(parts)


def failed_text(job, result) -> str:
    parts = [f"❌ 发布失败 #{job.id}"]
    if result.error:
        parts.append(f"原因：{result.error}")
    if result.backend:
        parts.append(f"后端：{result.backend}")
    parts.append(f"已尝试 {job.attempts} 次")
    if result.retryable:
        parts.append("该错误可重试（/retry 重置失败任务）")
    body = (job.tweet_text or job.raw_text or "").strip()
    if body:
        parts.append("")
        parts.append(body[:600])
    return "\n".join(parts)


# ── 实现 ─────────────────────────────────────────────────

class LogNotifier:
    """默认通知器：只写日志。永远可用，不依赖任何外部服务。"""

    async def sent(self, job, result) -> None:
        log.info("回执 #%s 已发布 %s（后端=%s）",
                 getattr(job, "id", "?"), result.tweet_url or result.tweet_id,
                 result.backend or "-")

    async def failed(self, job, result) -> None:
        log.warning("回执 #%s 发布失败 [%s] %s",
                    getattr(job, "id", "?"), result.backend or "-", result.error)


# 预览卡登记表：(chat_id, job_id) -> 卡片消息 id
# 用途：内容发出后把那张带 ✅/❌ 的卡片删掉，避免聊天里堆过期卡片。
# 用内存表而不是加数据库字段 —— 队列表属冻结文件，且卡片 id 本来就只在本次运行有意义。
_card_registry: dict[tuple[int, int], int] = {}


def note_card(chat_id: int, job_id: int, message_id: int) -> None:
    """登记一条预览卡（bot.py 发出预览后调用）。永远不抛异常。"""
    try:
        if chat_id and job_id and message_id:
            _card_registry[(int(chat_id), int(job_id))] = int(message_id)
    except Exception:
        pass


def forget_card(chat_id: int, job_id: int) -> None:
    """忘掉登记（取消/删除时调用）。"""
    try:
        _card_registry.pop((int(chat_id), int(job_id)), None)
    except Exception:
        pass


class TelegramNotifier:
    """把回执回发到原 Telegram 消息。

    另外负责**清理**：确认模式下的预览卡（那条带 ✅/❌ 的消息）在内容
    发出（或取消）后应当消失，否则聊天里会堆一堆过期卡片。
    卡片 id 由 bot.py 在发出预览时登记（`note_card()`）。

    约定：
      * `bot` 为 None（只跑 Web 控制台）时**静默跳过**，不报错。
      * `tg_chat_id == 0`（Web 手工投料）时跳过 —— 没有可回发的会话。
      * 回执用 `reply_to_message_id` + `allow_sending_without_reply=True`：
        原消息可能已被删除，这不该让整条回执失败。
      * 发送失败只记警告，绝不抛出。
    """

    def __init__(self, bot: Any | None = None) -> None:
        self.bot = bot

    @property
    def enabled(self) -> bool:
        return self.bot is not None

    async def remove_card(self, chat_id: int, message_id: int) -> bool:
        """删掉一条消息（预览卡）。失败只记日志，不抛异常。"""
        if self.bot is None or not chat_id or not message_id:
            return False
        try:
            await self.bot.delete_message(chat_id=int(chat_id),
                                          message_id=int(message_id))
            return True
        except Exception as e:
            log.debug("删除消息 %s/%s 失败（忽略）：%s", chat_id, message_id, e)
            return False

    async def sent(self, job, result) -> None:
        """成功回执。带一个「🔗 查看推文」按钮（有链接时）。"""
        await self._send(job, sent_text(job, result), result=result, ok=True)
        await self._drop_card(job)

    async def failed(self, job, result) -> None:
        """失败回执。带「🔄 重试」/「❌ 删除」按钮，方便当场处理。"""
        await self._send(job, failed_text(job, result), result=result, ok=False)
        # 失败的**不删卡片**：还要靠它上面的「重试」按钮再操作

    async def _drop_card(self, job) -> None:
        """内容已发出 -> 删掉那张预览卡，保持聊天干净。

        卡片 id 存在模块级登记表里（`Job` 是冻结 dataclass，不能加字段）。
        """
        try:
            chat_id = int(getattr(job, "tg_chat_id", 0) or 0)
            job_id = int(getattr(job, "id", 0) or 0)
            card_id = _card_registry.get((chat_id, job_id), 0)
            if chat_id and card_id:
                await self.remove_card(chat_id, card_id)
                _card_registry.pop((chat_id, job_id), None)
        except Exception as e:
            log.debug("清理预览卡失败（忽略）：%s", e)

    @staticmethod
    def _keyboard(job, result, ok: bool):
        """按成败给不同按钮。任何异常都返回 None（回执不能因按钮挂掉）。"""
        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        except Exception:
            return None
        try:
            jid = int(getattr(job, "id", 0) or 0)
            rows = []
            url = getattr(result, "tweet_url", "") or ""
            if not url:
                tid = getattr(result, "tweet_id", "") or ""
                url = f"https://x.com/i/status/{tid}" if tid else ""
            if ok and url:
                rows.append([InlineKeyboardButton("🔗 查看推文", url=url)])
            if not ok and jid:
                rows.append([
                    InlineKeyboardButton("🔄 重试", callback_data=f"retry:{jid}"),
                    InlineKeyboardButton("❌ 删除", callback_data=f"no:{jid}"),
                ])
            return InlineKeyboardMarkup(rows) if rows else None
        except Exception:
            return None

    async def _send(self, job, text: str, *, result=None, ok: bool = True) -> None:
        if self.bot is None:
            log.debug("#%s 无 Telegram bot，跳过回执", getattr(job, "id", "?"))
            return
        chat_id = int(getattr(job, "tg_chat_id", 0) or 0)
        if chat_id == 0:
            log.debug("#%s 无来源会话（Web 投料），跳过回执", getattr(job, "id", "?"))
            return
        try:
            kb = self._keyboard(job, result, ok) if result is not None else None
            await self.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_to_message_id=int(getattr(job, "tg_msg_id", 0) or 0) or None,
                allow_sending_without_reply=True,
                reply_markup=kb,
            )
        except Exception as e:
            log.warning("#%s 回执发送失败（忽略）：%s: %s",
                        getattr(job, "id", "?"), type(e).__name__, e)


class MultiNotifier:
    """把回执扇出给多个通知器。单个异常不影响其它。"""

    def __init__(self, notifiers: Iterable[Any] | None = None) -> None:
        self.notifiers: list[Any] = list(notifiers or [])

    def add(self, notifier: Any) -> None:
        self.notifiers.append(notifier)

    async def sent(self, job, result) -> None:
        await self._fanout("sent", job, result)

    async def failed(self, job, result) -> None:
        await self._fanout("failed", job, result)

    async def _fanout(self, event: str, job, result) -> None:
        for n in self.notifiers:
            try:
                await getattr(n, event)(job, result)
            except Exception as e:
                log.warning("通知器 %s.%s 失败（忽略）：%s: %s",
                            type(n).__name__, event, type(e).__name__, e)
