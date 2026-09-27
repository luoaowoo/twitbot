"""热评采集（新文件）。

为什么只要「几条」：用户明确要求**热评不要太多** ——
评论区本身的梗在于「一句顶一万句」，堆十几条反而没笑点。
默认只取 **Top 3**（含楼中楼第一条回复）。

实测（2026-09-27）：
  * B站 **不需要登录**：``api.bilibili.com/x/v2/reply?type=1&oid=<aid>&sort=2``
    sort=2 = 按热度，返回带 like 的评论 + 楼中楼 replies
    实测某热门视频：897 条评论，Top 是 ♥234「找到主人家了」/ ♥155 / ♥46
  * 小红书要开笔记详情页（评论是登录后异步加载的），走浏览器
  * 微博评论接口风控严，暂不支持

统一返回 [{author, text, likes, replies:[{author,text,likes}]}]
"""
from __future__ import annotations

import json
import logging
import urllib.request

from .base import parse_count

log = logging.getLogger("twitbot.collect.comments")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

MAX_COMMENTS = 3          # 默认只留 3 条 —— 多了就没笑点
MAX_REPLIES = 1           # 每条热评最多带 1 条楼中楼
MAX_LEN = 120             # 单条评论文本上限


def _get_json(url: str, referer: str, timeout: int = 20) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Referer": referer,
            "Accept": "application/json, text/plain, */*",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        log.debug("热评请求失败 %s: %s", type(e).__name__, e)
        return None


def _clip(s: str) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= MAX_LEN else s[:MAX_LEN] + "…"


def bilibili_comments(item_id: str, *, limit: int = MAX_COMMENTS) -> list[dict]:
    """B站热评（免登录）。item_id 是 aid。"""
    aid = "".join(ch for ch in str(item_id or "") if ch.isdigit())
    if not aid:
        return []
    url = (f"https://api.bilibili.com/x/v2/reply?type=1&oid={aid}"
           f"&sort=2&ps={max(1, limit)}&pn=1")
    data = _get_json(url, "https://www.bilibili.com/")
    if not data or data.get("code") != 0:
        return []
    out = []
    for r in ((data.get("data") or {}).get("replies") or [])[:max(1, limit)]:
        try:
            content = (r.get("content") or {}).get("message") or ""
            if not content.strip():
                continue
            subs = []
            for s2 in ((r.get("replies") or [])[:MAX_REPLIES]):
                sc = (s2.get("content") or {}).get("message") or ""
                if sc.strip():
                    subs.append({
                        "author": ((s2.get("member") or {}).get("uname") or "")[:20],
                        "text": _clip(sc),
                        "likes": parse_count(s2.get("like")),
                    })
            out.append({
                "author": ((r.get("member") or {}).get("uname") or "")[:20],
                "text": _clip(content),
                "likes": parse_count(r.get("like")),
                "replies": subs,
            })
        except Exception:
            continue
    # B站的 sort=2 名义上是「热度」，实测顺序并不严格按赞数（46/236/156），
    # 所以本地再按点赞重排一遍，保证「最热在前」是确定的。
    out.sort(key=lambda c: c.get("likes", 0), reverse=True)
    return out[:max(1, limit)]


def xiaohongshu_comments(note_id: str, *, limit: int = MAX_COMMENTS,
                         on_event=None) -> list[dict]:
    """小红书热评：开笔记详情页，读渲染出来的评论。

    评论是登录后异步加载的，所以要**带登录态**的浏览器会话。
    拿不到就返回空（不阻断主流程）。
    """
    nid = str(note_id or "").strip()
    if not nid:
        return []
    try:
        from ..backends.browser import BrowserBackend
    except Exception:
        return []
    be = BrowserBackend()
    js = r"""
    () => {
      const out = [];
      document.querySelectorAll('.comment-item, .comment-inner-container').forEach(el => {
        const name = el.querySelector('.name, .nickname');
        const txt  = el.querySelector('.content .note-text, .note-text, .content');
        const like = el.querySelector('.like .count, .like-wrapper .count');
        const t = txt ? (txt.innerText || '').trim() : '';
        if (!t) return;
        out.push({author: name ? (name.innerText||'').trim() : '',
                  text: t, likes: like ? (like.innerText||'').trim() : ''});
      });
      return out.slice(0, 10);
    }
    """
    try:
        with be._open_session(headless=True) as s:
            page = s.context.new_page()
            page.set_viewport_size({"width": 1280, "height": 1000})
            page.goto(f"https://www.xiaohongshu.com/explore/{nid}",
                      wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(6000)
            for _ in range(2):
                page.mouse.wheel(0, 2200)
                page.wait_for_timeout(2500)
            rows = page.evaluate(js) or []
    except Exception as e:
        log.debug("小红书热评失败: %s", type(e).__name__)
        return []
    rows.sort(key=lambda r: parse_count(r.get("likes")), reverse=True)
    out = []
    for r in rows[:max(1, limit)]:
        if not (r.get("text") or "").strip():
            continue
        out.append({"author": (r.get("author") or "")[:20],
                    "text": _clip(r["text"]),
                    "likes": parse_count(r.get("likes")),
                    "replies": []})
    return out


def fetch_for(item: dict, *, limit: int = MAX_COMMENTS,
              on_event=None) -> list[dict]:
    """按平台取热评。认不出平台/没有 id 就返回空。**绝不抛异常**。"""
    p = (item or {}).get("platform") or ""
    iid = (item or {}).get("item_id") or ""
    try:
        if p == "bilibili":
            return bilibili_comments(iid, limit=limit)
        if p == "xiaohongshu":
            return xiaohongshu_comments(iid, limit=limit, on_event=on_event)
    except Exception as e:
        log.debug("取热评异常 %s: %s", p, type(e).__name__)
    return []


def to_text(comments: list[dict], *, limit: int = MAX_COMMENTS) -> str:
    """把热评排成人看的一段字（给 TG 审核卡片用）。"""
    if not comments:
        return ""
    lines = []
    for c in comments[:max(1, limit)]:
        lines.append(f"♥{c.get('likes', 0)} {c.get('text', '')}")
        for r in (c.get("replies") or [])[:MAX_REPLIES]:
            lines.append(f"    └ ♥{r.get('likes', 0)} {r.get('text', '')}")
    return "\n".join(lines)
