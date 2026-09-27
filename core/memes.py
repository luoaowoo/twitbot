"""梗图模板库（新文件）—— 只管「底图」的存取。

**合成放在浏览器 Canvas 里做**，这个模块只负责模板文件：

  * 不引入 Pillow、不在服务器上渲染图片 —— 免依赖、免字体、免占 CPU
  * 浏览器里实时预览，所见即所得
  * 导出 PNG 后走**已有的** `/api/media/upload` 进 MEDIA_DIR，再照常投料

内置模板（纯色/渐变底）由前端 canvas 直接画，不落文件；
用户上传的模板存 ``DATA_DIR/memes/``，下次还能选。

安全：所有对外函数都做 basename 校验，拒绝 ``../``、绝对路径、
Windows 非法字符 —— 跟 core/media.py 一个口径。
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from . import config

log = logging.getLogger("twitbot.memes")

MAX_TEMPLATE_BYTES = 8 * 1024 * 1024          # 模板底图上限 8MB
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def memes_dir() -> Path:
    """模板目录（每次现取，方便测试里 patch config.DATA_DIR）。"""
    return Path(config.DATA_DIR) / "memes"


def safe_name(name: str) -> str:
    """把用户给的名字洗成安全的 basename。不合法返回空串。"""
    raw = (name or "").strip()
    if not raw:
        return ""
    # 只取最后一段，挡掉 ../、..\\、绝对路径
    raw = raw.replace("\\", "/").split("/")[-1]
    if raw in (".", "..") or _BAD_CHARS.search(raw):
        return ""
    stem, dot, ext = raw.rpartition(".")
    if not dot or not stem:
        return ""
    ext = "." + ext.lower()
    if ext not in ALLOWED_EXT:
        return ""
    stem = stem[:60]
    return f"{stem}{ext}"


def list_templates() -> list[dict]:
    """列出已上传的模板（新的在前）。**绝不抛异常**。"""
    try:
        d = memes_dir()
        if not d.is_dir():
            return []
        out = []
        for p in d.iterdir():
            if not p.is_file():
                continue
            if p.suffix.lower() not in ALLOWED_EXT:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({"name": p.name, "size": st.st_size,
                        "mtime": st.st_mtime,
                        "url": f"/api/memes/file?name={p.name}"})
        out.sort(key=lambda r: r["mtime"], reverse=True)
        return out
    except Exception as e:
        log.debug("列模板失败：%s", e)
        return []


def search(q: str, limit: int = 300) -> list[dict]:
    """按文件名模糊搜素材（大小写不敏感）。q 为空就返回全部。"""
    key = (q or "").strip().lower()
    rows = list_templates()
    if key:
        rows = [r for r in rows if key in r["name"].lower()]
    return rows[: max(1, min(int(limit or 300), 1000))]


def stats() -> dict:
    """素材库概况（给 UI 显示计数 / 占用）。"""
    rows = list_templates()
    return {"count": len(rows), "bytes": sum(r.get("size") or 0 for r in rows)}


def save_template(filename: str, data: bytes) -> tuple[bool, str, str]:
    """存一个模板。返回 (成功?, 提示, 最终文件名)。"""
    if not data:
        return False, "上传内容为空", ""
    if len(data) > MAX_TEMPLATE_BYTES:
        return False, f"模板过大（上限 {MAX_TEMPLATE_BYTES // 1024 // 1024}MB）", ""
    name = safe_name(filename or "")
    if not name:
        return False, "文件名不合法（只支持 png/jpg/jpeg/webp/gif/bmp）", ""
    # 重名就加时间戳，别互相覆盖
    try:
        d = memes_dir()
        d.mkdir(parents=True, exist_ok=True)
        target = d / name
        if target.exists():
            stem, ext = name.rsplit(".", 1)
            name = f"{stem}-{int(time.time())}.{ext}"
            target = d / name
        target.write_bytes(data)
        return True, "模板已保存", name
    except Exception as e:
        return False, f"保存失败：{type(e).__name__}: {e}", ""


def delete_template(name: str) -> tuple[bool, str]:
    """删一个模板。名字不合法/不存在都返回 (False, 原因)。"""
    safe = safe_name(name)
    if not safe:
        return False, "文件名不合法"
    try:
        p = memes_dir() / safe
        if not p.is_file():
            return False, "模板不存在"
        p.unlink()
        return True, "已删除"
    except Exception as e:
        return False, f"删除失败：{type(e).__name__}: {e}"


def template_path(name: str) -> Path | None:
    """取模板的绝对路径（校验过 basename）。"""
    safe = safe_name(name)
    if not safe:
        return None
    p = memes_dir() / safe
    return p if p.is_file() else None
