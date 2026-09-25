"""文案与推文处理（冻结）—— 纯函数，无副作用，可独立测试。"""
from __future__ import annotations

import re

URL_RE = re.compile(r"https?://[^\s<>\"')]+")
TWEET_ID_RE = re.compile(r"https?://(?:www\.)?(?:twitter\.com|x\.com)/[^/\s]+/status/(\d+)")
TME_RE = re.compile(r"https?://t\.me/\S+")

# X 字数折算权重=2 的码点区间（twitter-text 简化版）
_HEAVY = (
    (0x1100, 0x115F), (0x2329, 0x232A), (0x2E80, 0x303E), (0x3041, 0x33FF),
    (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xA000, 0xA4CF), (0xAC00, 0xD7A3),
    (0xF900, 0xFAFF), (0xFE10, 0xFE19), (0xFE30, 0xFE6F), (0xFF00, 0xFF60),
    (0xFFE0, 0xFFE6), (0x1F300, 0x1F64F), (0x1F900, 0x1F9FF),
    (0x20000, 0x2FFFD), (0x30000, 0x3FFFD),
)


def _is_heavy(ch: str) -> bool:
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in _HEAVY)


def weighted_len(text: str) -> int:
    """按 X 规则折算长度：CJK/emoji 计 2，任意 URL 一律计 23。"""
    plain = URL_RE.sub("", text or "")
    return sum(2 if _is_heavy(c) else 1 for c in plain) + 23 * len(URL_RE.findall(text or ""))


def first_tweet_id(text: str) -> str | None:
    m = TWEET_ID_RE.search(text or "")
    return m.group(1) if m else None


def strip_tme(text: str) -> str:
    """去掉 t.me 链接（Telegram 来源链接对 X 无意义）。"""
    return TME_RE.sub("", text or "").strip()


def compose(prefix: str, body: str, suffix: str, limit: int = 280) -> str:
    """正文压到 limit 以内；原文最后一个链接永远保留（当尾部）。"""
    body = strip_tme(body or "")
    links = URL_RE.findall(body)
    keep = links[-1] if links else ""
    head = body.replace(keep, "").strip() if keep else body
    head = re.sub(r"\s{2,}", " ", head)

    def build(h: str) -> str:
        top = " ".join(x for x in (prefix.strip(), h.strip()) if x)
        tail = " ".join(x for x in (keep, suffix.strip()) if x)
        return "\n".join(x for x in (top, tail) if x)

    original_head = head
    while head and weighted_len(build(head)) > limit:
        head = head[:-1].rstrip()
    if head != original_head:
        head = head + "…"
    return build(head)


def content_hash(kind: str, text: str, quote_id: str = "", media_name: str = "") -> str:
    """内容指纹，用于窗口去重。"""
    import hashlib
    return hashlib.sha256(f"{kind}|{text}|{quote_id}|{media_name}".encode()).hexdigest()[:32]
