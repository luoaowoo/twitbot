"""百度贴吧采集器。

实测（2026-09-27，服务器无头浏览器）：
  * 直接 HTTP 请求 ``tieba.baidu.com`` → **403 / 百度安全验证**
  * 但**真实浏览器**打开 ``tieba.baidu.com/index.html`` 能正常渲染
  * ``/hotrank`` 仍会被「百度安全验证」拦，所以默认走**吧内帖列表**：
    ``/f?kw=<吧名>`` —— 可以按吧名采（比如「梗图」「沙雕图」）

所以这个采集器：给一个或多个吧名 → 打开该吧 → 抓帖子标题/链接/回复数 + 首图。
只收**有图**的帖子（发到 X 必须有图）。
"""
from __future__ import annotations

import logging
import time
from urllib.parse import quote

from .base import Item, parse_count, write_meta, read_meta, first_text, norm_url

log = logging.getLogger("twitbot.collect.tieba")

PLATFORM = "tieba"
FORUM_URL = "https://tieba.baidu.com/f?kw={kw}&ie=utf-8"
DEFAULT_FORUMS = ["梗图", "沙雕图"]

# 百度安全验证是**概率性**触发（实测同一脚本一次通一次被拦），
# 所以靠「重试 + 退避」扛过去，而不是想办法绕。
RETRY_TIMES = 3
RETRY_WAIT = 8               # 秒


def _blocked(page) -> bool:
    """页面是不是被百度安全验证拦了。"""
    try:
        title = page.title() or ""
        if "安全验证" in title:
            return True
        txt = page.evaluate("() => (document.body.innerText||'').slice(0,300)")
        return "安全验证" in (txt or "")
    except Exception:
        return False

# 帖列表：每条 li.j_thread_list
_LIST_JS = r"""
() => {
  const out = [];
  // 实测结构（2026-09-27，贴吧吧页）：
  //   div.thread-card > div.thread-card-wrapper
  //       |-- a.thread-content-link   <- 帖子链接
  //       |-- .thread-title           <- 标题
  //       |-- .thread-image img       <- 缩略图
  //       \-- .thread-action-area     <- 回复/点赞数
  // 坑：以前用 a[href*="/p/"] 取 innerText，拿到的其实是作者名+时间，不是标题。
  document.querySelectorAll('.thread-card, .thread-card-wrapper').forEach(card => {
    const a = card.querySelector('a.thread-content-link') || card.querySelector('a[href*="/p/"]');
    if (!a) return;
    const href = (a.href || '').split('#')[0];
    if (!/^https?:\/\/tieba\.baidu\.com\/p\//.test(href)) return;
    const tEl = card.querySelector('.thread-title');
    const title = tEl ? (tEl.innerText || '').trim() : '';
    if (!title || title.length < 3) return;
    const img = card.querySelector('.thread-image img, img');
    // 操作区文本是「回复数 点赞数 …」混在一起（实测拿到过 111731106 这种
    // 拼接出来的假数字），只取**第一个数字**当回复数。
    const area = card.querySelector('.thread-action-area, .thread-action-bar');
    const rawNum = area ? (area.innerText || '') : '';
    const mNum = rawNum.match(/\d+/);
    const num = mNum ? mNum[0] : '';
    out.push({
      title: title,
      href: href,
      img: (img && /^https?:/.test(img.src || '')) ? img.src : '',
      replies: num.slice(0, 40),
      author: '',
    });
  });
  return out;
}
"""

# 帖子内部：取正文里的图（点进去才有）
_POST_JS = r"""
() => {
  const imgs = [];
  // 放宽：贴吧详情页结构多变，凡是正文区域里的图都收
  const sel = '.p_content img, .d_post_content img, .post_content img, ' +
              '[class*="post-content"] img, [class*="content"] img.BDE_Image, ' +
              'img.BDE_Image, .pb_content img';
  document.querySelectorAll(sel).forEach(im => {
    const s = im.getAttribute('src') || im.src || '';
    if (!/^https?:/.test(s)) return;
    if (/tb2\.bdstatic\.com\/tb\/editor\/images|static\.tieba\.baidu\.com\/tb\/|emotion|face/i.test(s)) return;
    if ((im.naturalWidth || 0) < 120) return;      // 表情/图标很小
    imgs.push(s);
  });
  const like = document.querySelector('.core_reply_tail .praise_num, .j_praise_num, [class*="praise"]');
  return {imgs: imgs.slice(0, 4), likes: like ? (like.innerText || '').trim() : ''};
}
"""


class TiebaCollector:
    """百度贴吧采集器。对外方法**绝不抛异常**。"""

    name = PLATFORM
    label = "百度贴吧"
    glyph = "贴"

    def status(self) -> dict:
        meta = read_meta(PLATFORM)
        return {"logged_in": True,          # 吧内列表免登录（有安全验证但浏览器可过）
                "account": meta.get("account") or "",
                "last_run": meta.get("last_run") or "",
                "last_error": meta.get("last_error") or "",
                "last_count": meta.get("last_count") or 0}

    def logged_in(self) -> bool:
        return True

    def fetch(self, *, min_likes: int = 0, pages: int = 1, keyword: str = "",
              on_event=None, with_image: int = 8, forums=None) -> list[Item]:
        """按吧名列表采帖子。keyword 为空就用默认吧。"""
        def emit(m):
            try:
                if on_event:
                    on_event(m)
            except Exception:
                pass

        names = [keyword.strip()] if (keyword or "").strip() else list(forums or DEFAULT_FORUMS)
        from ..backends.browser import BrowserBackend
        be = BrowserBackend()
        items: list[Item] = []
        seen = set()
        try:
            with be._open_session(headless=True) as s:
                page = s.context.new_page()
                page.set_viewport_size({"width": 1400, "height": 1000})
                for kw in names[:3]:
                    # 实测（2026-09-27）：**不要预热首页** —— 预热那一步自己被
                    # 百度安全验证拦，而且直接进吧页反而是通的。
                    # 拦截是概率性的，所以用「重试 + 退避」扛过去。
                    rows = []
                    for attempt in range(RETRY_TIMES):
                        emit(f"打开「{kw}」吧…" + (f"（第 {attempt + 1} 次）" if attempt else ""))
                        try:
                            page.goto(FORUM_URL.format(kw=quote(kw)),
                                      wait_until="domcontentloaded", timeout=60000)
                        except Exception:
                            continue
                        page.wait_for_timeout(5000)
                        if _blocked(page):
                            if attempt < RETRY_TIMES - 1:
                                emit(f"撞到安全验证，等 {RETRY_WAIT} 秒重试…")
                                time.sleep(RETRY_WAIT)
                                continue
                            emit(f"「{kw}」吧连试 {RETRY_TIMES} 次都被拦，跳过")
                            break
                        try:
                            rows = page.evaluate(_LIST_JS) or []
                        except Exception as e:
                            log.debug("解析贴吧列表失败: %s", e)
                        if rows:
                            break
                        if attempt < RETRY_TIMES - 1:
                            time.sleep(RETRY_WAIT)
                    emit(f"「{kw}」吧拿到 {len(rows)} 个帖子")
                    picked = 0
                    for r in rows:
                        if picked >= max(0, with_image):
                            break
                        href = r.get("href") or ""
                        if not href or href in seen:
                            continue
                        seen.add(href)
                        imgs, likes = [], 0
                        try:
                            page.goto(href, wait_until="domcontentloaded", timeout=45000)
                            page.wait_for_timeout(3500)
                            got = page.evaluate(_POST_JS) or {}
                            imgs = [norm_url(u) for u in (got.get("imgs") or [])]
                            likes = parse_count(got.get("likes"))
                        except Exception:
                            pass
                        picked += 1
                        if not imgs and r.get("img"):
                            # 详情页没取到图就退回列表缩略图 ——
                            # 总比整条丢掉强（缩略图小一点但能发）
                            imgs = [norm_url(r["img"])]
                        if not imgs:
                            continue
                        pid = href.split("/p/")[-1].split("?")[0] if "/p/" in href else href[-16:]
                        items.append(Item(
                            platform=PLATFORM, item_id=pid,
                            title=first_text(r.get("title")),
                            text=first_text(r.get("title")),
                            author=first_text((r.get("author") or "").split("\n")[0], kw + "吧"),
                            url=href, cover=imgs[0], images=imgs,
                            likes=likes,
                            comments=parse_count(r.get("replies")),
                            kind="image",
                        ))
                        time.sleep(0.5)
                    page.goto(FORUM_URL.format(kw=quote(kw)),
                              wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(2000)

            if not items:
                write_meta(PLATFORM, last_error="没采到带图的帖子（可能被安全验证拦了）")
                return []
            write_meta(PLATFORM, last_error="", last_count=len(items),
                       last_run=time.strftime("%Y-%m-%d %H:%M"))
            return items
        except Exception as e:
            log.warning("贴吧采集异常: %s", type(e).__name__)
            write_meta(PLATFORM, last_error=f"{type(e).__name__}: {e}")
            return []
