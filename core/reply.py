"""推文操作目标的编码（转帖 / 引用 / 评论）—— 纯函数，无副作用，可独立测试。

为什么需要这一层：
  * 评论和引用在 X 侧都走 `CreateTweet`，但参数不同
    （`in_reply_to_tweet_id` vs `quote_tweet_id`）；**转帖**走的是完全不同的
    `CreateRetweet` 调用，三者绝不能混。
  * 队列表 `jobs` 是冻结文件，不能再加列。`quote_id` 一列因此承载三种含义，
    一律用前缀区分：
        `r:<推文id>` = 评论目标
        `t:<推文id>` = 转帖（纯转推）目标
        纯数字        = 引用目标
  * `kind` **刻意保持内容类型**（text/photo/video/document）不变 ——
    core.media 的限额判定和视频分片上传都依赖它，改成操作名会算错限额。

⚠ 任何发布路径都必须先问本模块要目标，不要自己 `isdigit()` 判断 ——
   `r:123` / `t:123` 都不是数字，直接当引用目标会发出**形态完全错的推文**。
"""
from __future__ import annotations

#: 评论目标前缀。选 `r:` 是因为推文 id 全为数字，不会撞车。
REPLY_PREFIX = "r:"
#: 转帖目标前缀。
RETWEET_PREFIX = "t:"


def _norm(tweet_id: object) -> str:
    try:
        return str(tweet_id or "").strip()
    except Exception:
        return ""


# ── 评论 ──────────────────────────────────────────────────

def make_reply_target(tweet_id: object) -> str:
    """把推文 id 编码成「评论目标」；输入为空时返回空串。"""
    tid = _norm(tweet_id)
    return f"{REPLY_PREFIX}{tid}" if tid else ""


def is_reply_target(stored: object) -> bool:
    return _norm(stored).startswith(REPLY_PREFIX)


def reply_target_id(stored: object) -> str:
    """取出评论目标推文 id；不是评论任务返回空串。"""
    s = _norm(stored)
    return s[len(REPLY_PREFIX):].strip() if s.startswith(REPLY_PREFIX) else ""


# ── 转帖 ──────────────────────────────────────────────────

def make_retweet_target(tweet_id: object) -> str:
    """把推文 id 编码成「转帖目标」；输入为空时返回空串。"""
    tid = _norm(tweet_id)
    return f"{RETWEET_PREFIX}{tid}" if tid else ""


def is_retweet_target(stored: object) -> bool:
    return _norm(stored).startswith(RETWEET_PREFIX)


def retweet_target_id(stored: object) -> str:
    """取出转帖目标推文 id；不是转帖任务返回空串。"""
    s = _norm(stored)
    return s[len(RETWEET_PREFIX):].strip() if s.startswith(RETWEET_PREFIX) else ""


# ── 引用 ──────────────────────────────────────────────────

def quote_target_id(stored: object) -> str:
    """取出「引用」目标；是评论/转帖任务或为空时返回空串。

    ⚠ 引用路径必须用本函数而不是直接读 `quote_id`，否则会把评论或转帖
      目标当成引用目标，发出错误的内容。
    """
    s = _norm(stored)
    if not s or s.startswith(REPLY_PREFIX) or s.startswith(RETWEET_PREFIX):
        return ""
    return s


# ── 通用 ──────────────────────────────────────────────────

def target_kind(stored: object) -> str:
    """返回该任务的目标类型：`reply` / `retweet` / `quote` / ""（无目标）。"""
    s = _norm(stored)
    if not s:
        return ""
    if s.startswith(REPLY_PREFIX):
        return "reply"
    if s.startswith(RETWEET_PREFIX):
        return "retweet"
    return "quote"


def target_id(stored: object) -> str:
    """不管是哪种目标，都取出其中的推文 id；无目标返回空串。"""
    kind = target_kind(stored)
    if kind == "reply":
        return reply_target_id(stored)
    if kind == "retweet":
        return retweet_target_id(stored)
    if kind == "quote":
        return quote_target_id(stored)
    return ""


def tweet_url(tweet_id: object) -> str:
    """拼一条推文链接；id 为空时返回空串。"""
    tid = _norm(tweet_id)
    return f"https://x.com/i/status/{tid}" if tid else ""
