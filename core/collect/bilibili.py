"""B站采集器：只用公开接口，**不需要登录**。

实测（2026-09-27，服务器直连）：
  * ``api.bilibili.com/x/web-interface/popular``            → 200，热门视频列表
  * ``api.bilibili.com/x/web-interface/ranking/v2``         → 200，排行榜
  * ``api.bilibili.com/x/web-interface/search/type``        → 200，关键词搜索

注意：B站是**视频**站，热门内容以视频为主。要发到 X 只能：
  * 用封面图（pic 字段，直链可下载）—— 默认这么做
  * 或者以后加「下载视频」（体积大，先不做）
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request

from .base import Item, parse_count, write_meta, read_meta, first_text, norm_url

log = logging.getLogger("twitbot.collect.bili")

PLATFORM = "bilibili"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
API_POPULAR = "https://api.bilibili.com/x/web-interface/popular?ps={ps}&pn={pn}"
API_SEARCH = ("https://api.bilibili.com/x/web-interface/search/type"
              "?search_type=video&keyword={kw}&page={pn}")


def _http_json(url: str, timeout: int = 20) -> dict | None:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": "https://www.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        log.debug("B站请求失败 %s: %s", type(e).__name__, e)
        return None


class BilibiliCollector:
    """B站采集器。无需登录。对外方法**绝不抛异常**。"""

    name = PLATFORM
    label = "B站"
    glyph = "哔"

    def status(self) -> dict:
        meta = read_meta(PLATFORM)
        return {"logged_in": True,            # 不需要登录，永远「可用」
                "account": "", "last_run": meta.get("last_run") or "",
                "last_error": meta.get("last_error") or "",
                "last_count": meta.get("last_count") or 0}

    def logged_in(self) -> bool:
        return True

    def fetch(self, *, min_likes: int = 0, pages: int = 2, keyword: str = "",
              on_event=None) -> list[Item]:
        emit = lambda m: (_safe(on_event, m))
        items: dict[str, Item] = {}
        try:
            for pn in range(1, max(1, pages) + 1):
                if keyword:
                    emit(f"搜「{keyword}」第 {pn} 页…")
                    url = API_SEARCH.format(kw=urllib.parse.quote(keyword), pn=pn)
                else:
                    emit(f"取热门第 {pn} 页…")
                    url = API_POPULAR.format(ps=20, pn=pn)
                data = _http_json(url)
                if not data or data.get("code") != 0:
                    emit("接口没返回数据（可能被限流）")
                    break
                rows = ((data.get("data") or {}).get("list")
                        or (data.get("data") or {}).get("result") or [])
                if not rows:
                    break
                for r in rows:
                    it = self._to_item(r)
                    if it and it.item_id:
                        items[it.key()] = it
            if not items:
                write_meta(PLATFORM, last_error="没采到内容")
                return []
            write_meta(PLATFORM, last_error="", last_count=len(items))
            return list(items.values())
        except Exception as e:
            log.warning("B站采集异常: %s", type(e).__name__)
            write_meta(PLATFORM, last_error=f"{type(e).__name__}: {e}")
            return []

    def _to_item(self, r: dict) -> Item | None:
        try:
            # 热门接口用 aid/bvid；搜索接口用 bvid + arcurl
            bvid = str(r.get("bvid") or "").strip()
            aid = str(r.get("aid") or r.get("id") or "").strip()
            iid = bvid or aid
            if not iid:
                return None
            pic = r.get("pic") or ""
            if pic.startswith("//"):
                pic = "https:" + pic
            pic = re.sub(r"@[^/]*$", "", pic)        # 去掉 @320w_200h 这类缩放后缀
            url = r.get("arcurl") or (f"https://www.bilibili.com/video/{bvid}" if bvid else "")
            title = re.sub(r"<[^>]+>", "", first_text(r.get("title")))   # 搜索接口会带 <em>
            return Item(
                platform=PLATFORM, item_id=iid,
                title=title, text=first_text(r.get("desc"), title),
                author=first_text(r.get("owner", {}).get("name") if isinstance(r.get("owner"), dict) else "",
                                  r.get("author")),
                url=url, cover=norm_url(pic),
                images=[norm_url(pic)] if pic else [],
                likes=parse_count(r.get("stat", {}).get("like") if isinstance(r.get("stat"), dict)
                                  else r.get("like")),
                comments=parse_count(r.get("stat", {}).get("reply")
                                     if isinstance(r.get("stat"), dict) else 0),
                collects=parse_count(r.get("stat", {}).get("favorite")
                                     if isinstance(r.get("stat"), dict) else 0),
                kind="video",
                created_at=str(r.get("pubdate") or ""),
            )
        except Exception:
            return None


def _safe(fn, msg):
    try:
        if fn:
            fn(msg)
    except Exception:
        pass
