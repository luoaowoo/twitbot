"""采集器公共部分：数据结构 / 数字解析 / 登录态存取。

放这里的东西必须**平台无关**：小红书、B站、微博都复用同一套。
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from .. import config, queue

log = logging.getLogger("twitbot.collect")

STATE_FILENAME = "storage_state.json"


class CollectorError(RuntimeError):
    """采集器内部的错误（对外会被吞成 (False, msg)）。"""


# ══════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════

@dataclass
class Item:
    """一条采集到的内容。各平台都归一到这个形状。"""

    platform: str                  # xiaohongshu / bilibili / ...
    item_id: str                   # 平台内唯一 id
    title: str = ""
    text: str = ""                 # 正文/简介
    author: str = ""
    url: str = ""                  # 原帖地址（转发时带上做「出处」）
    cover: str = ""                # 封面图直链
    images: list[str] = field(default_factory=list)   # 全部图片直链
    likes: int = 0
    comments: int = 0
    collects: int = 0              # 收藏数
    shares: int = 0
    kind: str = "image"            # image | video
    created_at: str = ""
    fetched_at: str = ""

    def key(self) -> str:
        return f"{self.platform}:{self.item_id}"

    def to_row(self) -> dict:
        d = asdict(self)
        d["key"] = self.key()
        d["images"] = json.dumps(self.images or [], ensure_ascii=False)
        return d

    def heat(self) -> int:
        """热度分：点赞为主，评论/收藏次之（用于排序和阈值）。"""
        return int(self.likes) + int(self.comments) * 3 + int(self.collects) * 2


# ══════════════════════════════════════════════════════════
# 数字解析（小红书/微博会回 "1.6万" "3.2w" "1,234" 这种）
# ══════════════════════════════════════════════════════════

_UNIT = {"万": 10_000, "w": 10_000, "W": 10_000, "k": 1_000, "K": 1_000,
         "千": 1_000, "亿": 100_000_000}


def parse_count(v: Any) -> int:
    """把 "1.6万" / "3.2w" / "1,234" / "999+" / 数字 统一成 int。"""
    if v is None:
        return 0
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().replace(",", "").replace("+", "").replace(" ", "")
    if not s:
        return 0
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*([万wWkK千亿]?)", s)
    if not m:
        return 0
    try:
        num = float(m.group(1))
    except ValueError:
        return 0
    unit = m.group(2)
    return int(num * _UNIT.get(unit, 1))


# ══════════════════════════════════════════════════════════
# 登录态
# ══════════════════════════════════════════════════════════

def collect_dir() -> Path:
    """采集相关的数据目录：DATA_DIR/collect/。"""
    return Path(config.DATA_DIR) / "collect"


def state_path(platform: str) -> Path:
    """某个平台的登录态文件（Playwright storage_state 格式）。"""
    return collect_dir() / platform / STATE_FILENAME


def has_state(platform: str) -> bool:
    p = state_path(platform)
    try:
        if not p.is_file() or p.stat().st_size < 20:
            return False
        data = json.loads(p.read_text(encoding="utf-8"))
        return bool(isinstance(data, dict) and data.get("cookies"))
    except Exception:
        return False


def read_state(platform: str) -> dict | None:
    try:
        p = state_path(platform)
        if not p.is_file():
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_state(platform: str, data: dict) -> bool:
    """存登录态（权限尽量收紧）。"""
    try:
        p = state_path(platform)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        try:
            import os
            os.chmod(p, 0o600)
        except Exception:
            pass
        return True
    except Exception as e:
        log.warning("写登录态失败 %s: %s", platform, e)
        return False


def clear_state(platform: str) -> bool:
    try:
        p = state_path(platform)
        if p.is_file():
            p.unlink()
        return True
    except Exception:
        return False


def state_summary(platform: str) -> str:
    """登录态摘要（cookie 数 / 保存时间）。不回传 cookie 内容。"""
    data = read_state(platform)
    if not data:
        return "未登录"
    n = len(data.get("cookies") or [])
    try:
        when = time.strftime("%Y-%m-%d %H:%M",
                             time.localtime(state_path(platform).stat().st_mtime))
    except Exception:
        when = "?"
    return f"{n} 条 cookie · 保存于 {when}"


# ══════════════════════════════════════════════════════════
# 采集记录（每个平台上次跑的情况）
# ══════════════════════════════════════════════════════════

def _meta_path(platform: str) -> Path:
    return collect_dir() / platform / "meta.json"


def read_meta(platform: str) -> dict:
    try:
        p = _meta_path(platform)
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def write_meta(platform: str, **kw) -> None:
    try:
        p = _meta_path(platform)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = read_meta(platform)
        data.update(kw)
        data["updated_at"] = queue.now_iso()
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log.debug("写 meta 失败 %s: %s", platform, e)


def norm_url(u: str) -> str:
    """把平台给的图片 URL 规整一下（去重/去掉过长的签名参数时留个心眼）。"""
    return (u or "").strip().replace("http://", "https://", 1) \
        if (u or "").startswith("http://") else (u or "").strip()


def first_text(*vals: Any) -> str:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""
