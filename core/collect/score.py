"""采集内容的筛选打分（新文件）。

依据：用户给的参考号 @YongQuan / @XUEQIUxka（同一对账号）实测 ——
  * 图片比例：0.56(竖) / 0.92(方) / 0.99(方) / 1.23(横) —— **竖图方图为主**
  * 配文：4~22 字，是人写的**吐槽**，不是原帖标题那种长描述
  * 一条推文 **1~2 张图**
  * 带「评论账号」标记 → 内容价值常在**评论区**

**两层机制**：
  1. ``hard_reject()`` —— 硬门槛，不满足直接丢（没图 / logo / 明显横图 / 零赞）
  2. ``score()`` —— 打分，越高越先推给管理员审核

横图门槛定在 **1.6**：1.23 那条置顶要能过；16:9(1.78) 和全景图直接丢；
3:2(1.5) 是手机常见比例，不硬砍，交给打分让它排在竖图后面。
"""
from __future__ import annotations

import math

# 硬门槛
MAX_RATIO = 1.6              # 宽/高 超过这个 = 明显横图，丢
MIN_LIKES = 1                # 零赞的不要

# 图片 URL 里出现这些基本是站标/头像/图标，不是内容图
JUNK_URL = ("logo", "avatar", "icon", "sprite", "placeholder", "default",
            "weibologo", "timeline_card_small", "thumb150", "square")

# 明显是广告的用词
AD_WORDS = ("点击链接", "点击购买", "优惠券", "限时", "加微信", "私信我",
            "扫码", "领取", "包邮", "折扣", "促销", "代购", "vx:", "v信")


def looks_junk(url: str) -> bool:
    low = (url or "").lower()
    return any(k in low for k in JUNK_URL)


def looks_ad(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in AD_WORDS)


def pick_ratio(item: dict) -> tuple[int, int] | None:
    """取这条内容的主图宽高。优先用采集时量好的，没有就算了。"""
    wh = item.get("wh") or item.get("_wh")
    if isinstance(wh, (list, tuple)) and len(wh) == 2:
        try:
            w, h = int(wh[0]), int(wh[1])
            if w > 0 and h > 0:
                return w, h
        except Exception:
            pass
    return None


def hard_reject(item: dict) -> str:
    """返回拒绝原因；能过就返回空串。"""
    imgs = list(item.get("images") or [])
    if not imgs and item.get("cover"):
        imgs = [item["cover"]]
    if not imgs:
        return "没有图片"
    if all(looks_junk(u) for u in imgs):
        return "只有站标/头像类图片"
    if looks_ad(item.get("title") or item.get("text") or ""):
        return "疑似广告"
    if int(item.get("likes") or 0) < MIN_LIKES:
        return "零赞"
    wh = pick_ratio(item)
    if wh and wh[0] / max(1, wh[1]) > MAX_RATIO:
        return f"明显横图（{wh[0]}x{wh[1]}）"
    return ""


def score(item: dict) -> tuple[int, list[str]]:
    """打分 + 说明（说明给管理员看，解释为什么推荐这条）。"""
    pts, why = 0, []
    wh = pick_ratio(item)
    likes = int(item.get("likes") or 0)
    comments = int(item.get("comments") or 0)
    title = (item.get("title") or item.get("text") or "").strip()
    n_img = len(item.get("images") or ([item["cover"]] if item.get("cover") else []))

    # 竖图/方图 —— 参考号几乎全是这种，信息流里占屏大
    if wh:
        r = wh[0] / max(1, wh[1])
        if r <= 1.05:
            pts += 30; why.append("竖图/方图 +30")
        elif r <= 1.3:
            pts += 8; why.append("轻微横图 +8")
        else:
            pts -= 5; why.append("横图 -5")
        if wh[0] >= 800:
            pts += 10; why.append("高清 +10")
    # 热度（取对数，别让百万赞的霸榜）
    if likes > 0:
        g = int(math.log10(likes) * 15)
        pts += g; why.append(f"热度 +{g}")
    # 评论区才是梗
    if comments > 0:
        pts += 20; why.append("有讨论 +20")
    # 配文要短（参考号 4~22 字）
    if title:
        n = len(title)
        if n <= 22:
            pts += 10; why.append("配文短 +10")
        elif n > 60:
            pts -= 15; why.append("标题过长 -15")
    # 一条推文 1~2 张
    if n_img > 2:
        pts -= 10; why.append(f"{n_img} 张图 -10")
    # 水印特征
    if any("xhscdn" in (u or "") for u in (item.get("images") or [])):
        pts -= 5; why.append("小红书水印 -5")
    return pts, why


def evaluate(item: dict) -> dict:
    """一次算完：{ok, score, reasons, reject}。"""
    reason = hard_reject(item)
    if reason:
        return {"ok": False, "score": -999, "reasons": [], "reject": reason}
    s, why = score(item)
    return {"ok": True, "score": s, "reasons": why, "reject": ""}
