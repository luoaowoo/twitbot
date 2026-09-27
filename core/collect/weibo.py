"""微博采集器：走 **m.weibo.cn**（移动版）。

实测（2026-09-27，服务器无头浏览器）：
  * ``s.weibo.com/top/summary`` 热搜榜免登录能拿（51 条 + 热度值），
    但热搜条目只有话题词**没有图**，发不到 X。
  * ``s.weibo.com/weibo?q=...`` 搜索页会**跳登录** —— 所以 PC 搜索页这条路走不通。
  * **``m.weibo.cn`` 免登录就能用**：实测标题=微博、卡片 60、大图 25、无登录墙，
    接口是 ``/api/container/getIndex``（热门流）。

所以采集走 m.weibo.cn + **拦截法**：监听它自己的 ``container/getIndex``
响应，直接解析结构化 JSON（图片/正文/点赞/评论），比抓 DOM 稳得多。
这招在 `core/analytics.py`（X 日报）和 `core/collect/xiaohongshu.py` 里都用过。

另外：微博**扫码登录**也是可用的（``passport.weibo.com/sso/signin`` 上有二维码），
以后要采「关注流」「搜索」时再接。
"""
from __future__ import annotations

import logging
import re
import time

from .base import Item, parse_count, write_meta, read_meta, first_text, norm_url

log = logging.getLogger("twitbot.collect.weibo")

PLATFORM = "weibo"
M_HOME = "https://m.weibo.cn/"
HOT_URL = "https://s.weibo.com/top/summary"
LOGIN_URL = "https://passport.weibo.com/sso/signin"

# 热搜榜（免登录，作为「话题源」，但没图）
_HOT_JS = r"""
() => {
  const out = [];
  document.querySelectorAll('#pl_top_realtimehot table tbody tr').forEach(tr => {
    const a = tr.querySelector('td.td-02 a');
    const num = tr.querySelector('td.td-01 span');
    const title = a ? (a.innerText || '').trim() : '';
    if (!title) return;
    out.push({title: title, hot: num ? (num.innerText || '').trim() : '',
              href: a ? (a.getAttribute('href') || '') : ''});
  });
  return out;
}
"""


def _pics_from_mblog(mb: dict) -> list[str]:
    """把微博的图画成直链。

    实测（2026-09-27）：m.weibo.cn 的响应里 **``pics`` 经常是空的**，
    图在 ``pic_ids`` 里（只有 id）。所以要按微博的固定图床模板拼：
        https://wx{N}.sinaimg.cn/large/{pic_id}.jpg
    ``pics`` 有的话优先用（那是带完整 URL 的老格式）。
    """
    out: list[str] = []
    for p in (mb.get("pics") or []):
        u = ((p.get("large") or {}).get("url") or p.get("url") or "").strip()
        if u:
            out.append(norm_url(u))
    if not out:
        for pid in (mb.get("pic_ids") or []):
            pid = str(pid or "").strip()
            if pid:
                out.append(f"https://wx1.sinaimg.cn/large/{pid}.jpg")
    if not out:
        u = (mb.get("original_pic") or mb.get("thumbnail_pic") or "").strip()
        if u:
            out.append(norm_url(u))
    # 去重、保序
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u); uniq.append(u)
    return uniq


def _walk_mblogs(node, out: list, depth: int = 0) -> None:
    """递归挖出所有「像微博帖子」的 dict。

    不按 cards/card_group 的具体层级取 —— 微博的响应嵌套经常变，
    直接认特征：有 id + 有 attitudes_count 或 pics 的就是一条 mblog。
    """
    if depth > 12 or node is None or len(out) > 200:
        return
    if isinstance(node, dict):
        # 一条 mblog 的特征：有 id + 有正文或作者或图。
        # （不能要求必须有 attitudes_count —— 实测热门流里不少卡片没有这个字段）
        if node.get("id") and (node.get("user") or node.get("pics")
                               or node.get("text") or node.get("attitudes_count")):
            out.append(node)
        for v in node.values():
            _walk_mblogs(v, out, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _walk_mblogs(v, out, depth + 1)


def _mblog_to_item(mb: dict, rank: int) -> Item | None:
    """把 m.weibo.cn 的一条 mblog 转成我们的 Item。"""
    try:
        mid = str(mb.get("id") or mb.get("mid") or "").strip()
        if not mid:
            return None
        raw_text = re.sub(r"<[^>]+>", "", mb.get("text") or "").strip()
        pics = _pics_from_mblog(mb)
        if not pics:
            return None                    # 没图发不了
        user = mb.get("user") or {}
        return Item(
            platform=PLATFORM, item_id=mid,
            title=raw_text[:60],
            text=raw_text[:300],
            author=first_text(user.get("screen_name")),
            url=f"https://m.weibo.cn/detail/{mid}",
            cover=pics[0], images=pics[:4],
            likes=parse_count(mb.get("attitudes_count")),
            comments=parse_count(mb.get("comments_count")),
            collects=parse_count(mb.get("reposts_count")),
            kind="image",
        )
    except Exception:
        return None


class WeiboCollector:
    """微博采集器（移动版热门流，免登录）。对外方法**绝不抛异常**。"""

    name = PLATFORM
    label = "微博"
    glyph = "微"

    def status(self) -> dict:
        meta = read_meta(PLATFORM)
        return {"logged_in": True,          # 移动版热门免登录
                "account": meta.get("account") or "",
                "last_run": meta.get("last_run") or "",
                "last_error": meta.get("last_error") or "",
                "last_count": meta.get("last_count") or 0}

    def logged_in(self) -> bool:
        return True

    def fetch(self, *, min_likes: int = 0, pages: int = 2, keyword: str = "",
              on_event=None, **kw) -> list[Item]:
        """采 m.weibo.cn 热门流。滚动几屏，拦截它自己的接口拿结构化数据。"""
        def emit(m):
            try:
                if on_event:
                    on_event(m)
            except Exception:
                pass

        from ..backends.browser import BrowserBackend
        be = BrowserBackend()
        picked: dict[str, Item] = {}
        try:
            with be._open_session(headless=True) as s:
                page = s.context.new_page()
                page.set_viewport_size({"width": 430, "height": 900})   # 移动版视口
                # 注意：**不要**在这里覆盖 User-Agent。实测覆盖成 iPhone UA 后
                # m.weibo.cn 反而不再调 /api/container/getIndex（拿不到数据）；
                # 用浏览器原生 UA + 窄视口就够了。

                def on_resp(resp):
                    if "/api/container/getIndex" not in resp.url:
                        return
                    try:
                        j = resp.json()
                    except Exception:
                        return
                    log.info("微博接口命中: cards=%s", len(((j.get("data") or {}).get("cards") or [])))
                    found = []
                    _walk_mblogs(j, found)
                    for mb in found:
                        it = _mblog_to_item(mb, len(picked))
                        if it:
                            picked[it.key()] = it
                page.on("response", on_resp)

                emit("打开微博移动版热门…")
                page.goto(M_HOME, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(7000)
                for i in range(max(1, pages)):
                    emit(f"滚动第 {i + 1} 屏…")
                    try:
                        page.evaluate("() => window.scrollBy(0, 2600)")
                    except Exception:
                        pass
                    page.wait_for_timeout(4000)

            if not picked:
                write_meta(PLATFORM, last_error="没采到带图内容")
                return []
            write_meta(PLATFORM, last_error="", last_count=len(picked),
                       last_run=time.strftime("%Y-%m-%d %H:%M"))
            return list(picked.values())
        except Exception as e:
            log.warning("微博采集异常: %s", type(e).__name__)
            write_meta(PLATFORM, last_error=f"{type(e).__name__}: {e}")
            return []

    # ── 热搜榜（免登录，只有话题词没图，作为补充信息用）──
    def hot_topics(self, on_event=None) -> list[dict]:
        from ..backends.browser import BrowserBackend
        be = BrowserBackend()
        try:
            with be._open_session(headless=True) as s:
                page = s.context.new_page()
                page.goto(HOT_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(6000)
                return page.evaluate(_HOT_JS) or []
        except Exception:
            return []
