"""小红书采集器：扫码登录 + 采集推荐流。

实测（2026-09-27，服务器无头浏览器）：
  * 登录二维码元素是 ``img.qrcode-img``，src 是 base64 dataURL —— 能直接给前端显示
  * **未登录**也能从 ``window.__INITIAL_STATE__`` 挖出 40+ 条 note（带 likedCount）
  * 卡片 DOM 结构：``section.note-item > div.footer``，
    标题 ``a.title span``、作者 ``.author-wrapper a.author``、点赞 ``.like-wrapper .count``
  * 登录成功后 ``/api/sns/web/v1/homefeed`` 才会返回结构化 JSON（字段更全）

扫码登录为什么可行：不用碰密码、不用过验证码，用户手机一扫就好；
登录态就是 Playwright 的 storage_state，跟 X 那套完全一致。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .base import (Item, has_state, read_meta, state_path, norm_url,
                   parse_count, write_meta, write_state, first_text)

log = logging.getLogger("twitbot.collect.xhs")

PLATFORM = "xiaohongshu"
HOME = "https://www.xiaohongshu.com/explore"
LOGIN_WAIT_SECONDS = 240
POLL_MS = 2000
SESSION_COOKIE = "web_session"

# 在页面里把 __INITIAL_STATE__ 挖成纯数据（避免 Vue 响应式对象循环引用）
_EXTRACT_JS = r"""
() => {
  const st = window.__INITIAL_STATE__;
  if (!st) return [];
  const out = [], seen = new Set();
  const txt = v => (typeof v === 'string' ? v : '');
  const push = (c) => {
    try {
      const id = c.noteId || c.note_id || c.id;
      if (!id || seen.has(String(id))) return;
      seen.add(String(id));
      const ii = c.interactInfo || c.interact_info || {};
      const cov = c.cover || {};
      const imgs = (c.imageList || c.image_list || []).map(x =>
        txt(x.urlDefault || x.url_default || x.url ||
            ((x.infoList || x.info_list || [])[0] || {}).url)).filter(Boolean);
      const u = c.user || {};
      out.push({
        id: String(id),
        title: txt(c.displayTitle || c.display_title),
        desc: txt(c.desc),
        type: txt(c.type),
        liked: ii.likedCount || ii.liked_count || '',
        comments: ii.commentCount || ii.comment_count || '',
        collects: ii.collectedCount || ii.collected_count || '',
        cover: txt(cov.urlDefault || cov.url_default || cov.url),
        images: imgs,
        author: txt(u.nickname || u.nickName),
        authorId: txt(u.userId || u.user_id),
      });
    } catch (e) {}
  };
  const walk = (n, d) => {
    if (d > 12 || !n) return;
    if (Array.isArray(n)) { for (const x of n) walk(x, d + 1); return; }
    if (typeof n === 'object') {
      const c = n.noteCard || n.note_card;
      if (c) push(c);
      for (const k in n) { try { walk(n[k], d + 1); } catch (e) {} }
    }
  };
  walk(st, 0);
  return out;
}
"""

# DOM 兜底（滚动后的第二屏 __INITIAL_STATE__ 不更新，只能读 DOM）
# 坑：不能用 [class*="author"] —— 它会先命中 div.author-wrapper，
#     那样作者名里会混进点赞数（实测踩过）。
_EXTRACT_DOM_JS = r"""
() => {
  const out = [];
  document.querySelectorAll('section.note-item').forEach(el => {
    try {
      const a   = el.querySelector('a.cover') || el.querySelector('a[href*="/explore/"]');
      const img = el.querySelector('a.cover img') || el.querySelector('img');
      const t   = el.querySelector('.footer a.title span')
                  || el.querySelector('a.title span') || el.querySelector('a.title');
      const au  = el.querySelector('.author-wrapper a.author') || el.querySelector('a.author');
      const lk  = el.querySelector('.like-wrapper .count') || el.querySelector('.like-wrapper span');
      const id  = a ? (a.getAttribute('href') || '').split('/').pop().split('?')[0] : '';
      out.push({
        id: id,
        title: t ? (t.innerText || '').trim() : '',
        cover: img ? (img.src || '') : '',
        author: au ? (au.innerText || '').trim() : '',
        liked: lk ? (lk.innerText || '').trim() : '',
        url: a ? a.href : ''
      });
    } catch (e) {}
  });
  return out;
}
"""


class XiaohongshuCollector:
    """小红书采集器。对外方法**绝不抛异常**。"""

    name = PLATFORM
    label = "小红书"
    glyph = "小"

    # ── 状态 ────────────────────────────────────────────
    def status(self) -> dict:
        meta = read_meta(PLATFORM)
        return {"logged_in": has_state(PLATFORM),
                "account": meta.get("account") or "",
                "last_run": meta.get("last_run") or "",
                "last_error": meta.get("last_error") or "",
                "last_count": meta.get("last_count") or 0}

    def logged_in(self) -> bool:
        return has_state(PLATFORM)

    # ── 扫码登录 ────────────────────────────────────────
    def login_start(self, on_event=None, force: bool = False) -> tuple[bool, str, str]:
        """开始扫码登录。

        **立刻返回，不阻塞**（重要）：以前这里 `ready.wait(90)` 会卡住 HTTP
        请求最多 90 秒 —— 浏览器/反代一超时，用户就永远看不到二维码。
        现在二维码由后台线程准备，前端轮询 /login/status 自己取。

        force=True 时先掐掉上一个流程再重开（二维码过期/卡住时用）。
        """
        if _LOGIN["running"] and not force:
            # 已经有一个在跑：把当前进度原样给它（可能二维码还没生成）
            return True, _LOGIN.get("message") or "登录进行中", _LOGIN.get("qr") or ""
        if _LOGIN["running"] and force:
            _LOGIN["cancel"] = True           # 让旧线程自己退出
            time.sleep(0.4)

        def emit(m):
            try:
                if on_event:
                    on_event(str(m))
            except Exception:
                pass

        ready = threading.Event()
        err: dict[str, str] = {}

        def worker():
            from ..backends.browser import BrowserBackend
            be = BrowserBackend()
            try:
                with be._open_session(headless=True, use_state=False) as s:
                    page = s.context.new_page()
                    page.set_viewport_size({"width": 1280, "height": 900})
                    emit("打开小红书登录页…")
                    page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(4000)
                    for sel in ('text=登录', 'button:has-text("登录")'):
                        try:
                            if page.locator(sel).count():
                                page.locator(sel).first.click(timeout=5000)
                                break
                        except Exception:
                            continue
                    page.wait_for_timeout(3500)
                    qr = ""
                    for _ in range(10):
                        try:
                            el = page.locator("img.qrcode-img").first
                            if el.count():
                                src = el.get_attribute("src") or ""
                                if src.startswith("data:image"):
                                    qr = src
                                    break
                        except Exception:
                            pass
                        page.wait_for_timeout(1200)
                    if not qr:
                        err["msg"] = "没找到登录二维码（小红书可能改版了）"
                        _LOGIN.update(running=False, ok=False, qr="", message=err["msg"])
                        ready.set()
                        return
                    _LOGIN.update(running=True, ok=None, qr=qr,
                                  message="请用小红书 App 扫码", started_at=time.time())
                    ready.set()
                    emit("二维码已生成，等待扫码…")

                    deadline = time.time() + LOGIN_WAIT_SECONDS
                    while time.time() < deadline:
                        if _LOGIN.get("cancel"):
                            _LOGIN.update(running=False, ok=False, qr="", message="已取消")
                            return
                        try:
                            names = {c.get("name") for c in s.context.cookies()}
                        except Exception:
                            names = set()
                        if SESSION_COOKIE in names:
                            emit("扫码成功，正在保存登录态…")
                            page.wait_for_timeout(2500)
                            if not write_state(PLATFORM, s.context.storage_state()):
                                _LOGIN.update(running=False, ok=False, qr="",
                                              message="登录成功但保存登录态失败")
                                return
                            who = self._read_account(page)
                            write_meta(PLATFORM, account=who)
                            _LOGIN.update(running=False, ok=True, qr="",
                                          message="登录成功" + (("：" + who) if who else ""))
                            emit("登录成功")
                            return
                        page.wait_for_timeout(POLL_MS)
                    _LOGIN.update(running=False, ok=False, qr="", message="二维码超时，请重试")
            except Exception as e:
                log.warning("小红书扫码登录异常: %s", type(e).__name__)
                err["msg"] = f"登录异常：{type(e).__name__}"
                _LOGIN.update(running=False, ok=False, qr="", message=err["msg"])
                ready.set()

        _LOGIN.update(running=True, ok=None, qr="", message="正在准备二维码…",
                      started_at=time.time(), cancel=False)
        threading.Thread(target=worker, name="xhs-login", daemon=True).start()
        # 等一小会儿让二维码尽量就绪（短，不阻塞请求），没好的话前端轮询会拿到
        ready.wait(timeout=8)
        return True, _LOGIN.get("message") or "正在准备二维码…", _LOGIN.get("qr") or ""

    def login_status(self) -> dict:
        # 卡死看门狗：线程异常退出时 running 可能没清掉，这里兜底
        started = _LOGIN.get("started_at") or 0
        if _LOGIN.get("running") and started and (time.time() - started) > LOGIN_WAIT_SECONDS + 90:
            _LOGIN.update(running=False, ok=False, qr="",
                          message="二维码已超时，请重新生成")
        return {"running": bool(_LOGIN.get("running")), "ok": _LOGIN.get("ok"),
                "message": _LOGIN.get("message") or "", "qr": _LOGIN.get("qr") or "",
                "logged_in": has_state(PLATFORM),
                "account": read_meta(PLATFORM).get("account") or ""}

    def login_cancel(self) -> tuple[bool, str]:
        _LOGIN["cancel"] = True
        return True, "已请求取消"

    def logout(self) -> tuple[bool, str]:
        from .base import clear_state
        clear_state(PLATFORM)
        write_meta(PLATFORM, account="")
        return True, "已清除登录态"

    def _read_account(self, page) -> str:
        try:
            info = page.evaluate("""() => {
              try {
                const st = window.__INITIAL_STATE__ || {};
                const u = st.user || {};
                const d = u.userInfo || u.user_info || {};
                return d.nickname || d.nickName || '';
              } catch (e) { return ''; }
            }""")
            return str(info or "")[:40]
        except Exception:
            return ""

    # ── 采集 ────────────────────────────────────────────
    def fetch(self, *, min_likes: int = 0, pages: int = 3, keyword: str = "",
              on_event=None) -> list[Item]:
        """跑一次采集。**绝不抛异常**，失败返回空列表。"""
        def emit(m):
            try:
                if on_event:
                    on_event(m)
            except Exception:
                pass

        from ..backends.browser import BrowserBackend
        be = BrowserBackend()
        items: dict[str, Item] = {}
        try:
            with be._open_session(headless=True) as s:
                page = s.context.new_page()
                page.set_viewport_size({"width": 1400, "height": 1000})
                target = (f"https://www.xiaohongshu.com/search_result?keyword={keyword}"
                          if keyword else HOME)
                emit("打开小红书" + ("搜索页…" if keyword else "探索页…"))
                page.goto(target, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(6000)

                for i in range(max(1, pages)):
                    emit(f"采集第 {i + 1} 屏…")
                    rows = []
                    try:
                        rows = page.evaluate(_EXTRACT_JS) or []
                    except Exception as e:
                        log.debug("解析 __INITIAL_STATE__ 失败: %s", e)
                    if not rows:
                        try:
                            rows = page.evaluate(_EXTRACT_DOM_JS) or []
                        except Exception:
                            rows = []
                    for r in rows:
                        it = self._to_item(r)
                        if it and it.item_id:
                            items[it.key()] = it
                    page.mouse.wheel(0, 3600)
                    page.wait_for_timeout(3800)

            if not items:
                write_meta(PLATFORM, last_error="没采到内容（可能需要登录）")
                return []
            write_meta(PLATFORM, last_error="", last_count=len(items),
                       last_run=time.strftime("%Y-%m-%d %H:%M"))
            return list(items.values())
        except Exception as e:
            log.warning("小红书采集异常: %s", type(e).__name__)
            write_meta(PLATFORM, last_error=f"{type(e).__name__}: {e}")
            return []

    def _to_item(self, r: dict) -> Item | None:
        try:
            iid = str(r.get("id") or "").strip()
            if not iid:
                return None
            imgs = [norm_url(u) for u in (r.get("images") or []) if u]
            cover = norm_url(r.get("cover") or (imgs[0] if imgs else ""))
            if cover and cover not in imgs:
                imgs.insert(0, cover)
            url = r.get("url") or f"https://www.xiaohongshu.com/explore/{iid}"
            return Item(
                platform=PLATFORM, item_id=iid,
                title=first_text(r.get("title"), (r.get("desc") or "")[:60]),
                text=first_text(r.get("desc"), r.get("title")),
                author=first_text(r.get("author")),
                url=url, cover=cover, images=imgs,
                likes=parse_count(r.get("liked")),
                comments=parse_count(r.get("comments")),
                collects=parse_count(r.get("collects")),
                kind="video" if str(r.get("type") or "").lower() == "video" else "image",
            )
        except Exception:
            return None


# 登录进行中的全局状态（同时只允许一个登录流程）
_LOGIN: dict[str, Any] = {
    "running": False, "ok": None, "message": "", "qr": "",
    "started_at": 0.0, "cancel": False,
}
