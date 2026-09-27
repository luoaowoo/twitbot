"""各平台热门梗图采集器（新包）。

设计目标：**从社交平台自动采集「点赞多 / 讨论多」的图文 → 筛选 → 转发布到 X**。

架构：
    collect/base.py   统一的数据结构（Item）、数字解析、登录态存取
    collect/<平台>.py 一个平台一个采集器，都实现同一套接口
    collect/__init__.py 注册表

每个采集器对外的三个动作：
    login_*()   拿登录二维码 / 检查登录状态 / 保存登录态
    fetch()     跑一次采集，返回 [Item, ...]
    status()    这个平台现在能不能用（有没有登录态等）

实测记录（2026-09-27，服务器上真跑）：
  * 小红书登录二维码在**无头浏览器下正常渲染**（img.qrcode-img，base64 dataURL）
  * 未登录也能从 `window.__INITIAL_STATE__` 拿到 41 条 note（带 liked_count）
  * 未登录时 DOM 有 30 个 note-item 卡片（封面/标题/作者/点赞数）
  * 登录后 feed 接口才会返回结构化 JSON（edith.xiaohongshu.com/api/sns/web/...）
  * B站有公开 JSON 接口，**不需要登录**：
    api.bilibili.com/x/web-interface/popular
  * 微博 / 百度贴吧 / 知乎 直接请求 → 403/401（要登录态）

硬约束：所有对外函数**绝不抛异常**，一律返回 (ok, ...) 或空列表。
"""
from __future__ import annotations

from .base import Item, parse_count, CollectorError  # noqa: F401

# 平台注册表：name -> (中文名, 是否需要登录, 说明)
PLATFORMS: dict[str, dict] = {
    "xiaohongshu": {
        "label": "小红书", "glyph": "小", "need_login": True,
        "hint": "图文梗图主产地；扫码登录后能采推荐流/搜索",
    },
    "bilibili": {
        "label": "B站", "glyph": "哔", "need_login": False,
        "hint": "公开接口，无需登录；但热门内容以视频为主",
    },
    "weibo": {
        "label": "微博", "glyph": "微", "need_login": True,
        "wip": True,
        "hint": "热搜榜能拿到（51条带热度），但话题页取图还没调通 —— 调试中",
    },
    "tieba": {
        "label": "百度贴吧", "glyph": "贴", "need_login": True,
        "wip": True,
        "hint": "能进吧页，但帖子列表解析还没调通 —— 调试中",
    },
}


def _load(name: str):
    """按名加载采集器类（延迟导入：某个平台坏掉不影响别的）。"""
    key = (name or "").strip().lower()
    try:
        if key == "xiaohongshu":
            from .xiaohongshu import XiaohongshuCollector
            return XiaohongshuCollector
        if key == "bilibili":
            from .bilibili import BilibiliCollector
            return BilibiliCollector
        if key == "weibo":
            from .weibo import WeiboCollector
            return WeiboCollector
        if key == "tieba":
            from .tieba import TiebaCollector
            return TiebaCollector
    except Exception:          # 模块还没写 / 依赖缺失 → 当成「没实现」
        return None
    return None


def get_collector(name: str):
    """拿到某个平台的采集器实例；不支持/加载失败返回 None。"""
    cls = _load(name)
    if cls is None:
        return None
    try:
        return cls()
    except Exception:
        return None


def supported() -> list[str]:
    return list(PLATFORMS)


def describe() -> list[dict]:
    """给控制台用的平台清单（含各自的状态）。"""
    out = []
    for name, meta in PLATFORMS.items():
        row = {"name": name, "label": meta["label"], "glyph": meta["glyph"],
               "need_login": meta["need_login"], "hint": meta["hint"],
               "wip": bool(meta.get("wip")),
               "implemented": _load(name) is not None,
               "logged_in": False, "last_run": "", "last_error": ""}
        col = get_collector(name)
        if col is not None:
            try:
                st = col.status()
                row.update({k: st.get(k, row.get(k)) for k in
                            ("logged_in", "last_run", "last_error", "account")})
            except Exception:
                pass
        out.append(row)
    return out
